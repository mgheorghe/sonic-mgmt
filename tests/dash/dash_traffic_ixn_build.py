#!/usr/bin/env python3
"""Build the DASH per-ENI traffic config on IxNetwork *live* via RestPy.

This is the "create the config from scratch" alternative to loading ``bg.ixncfg``.
It programmatically builds, for one DPU, an outbound (VXLAN) raw traffic item
with 32 per-ENI flows — one unique VLAN each — using the exact same arithmetic
as ``configs/dash_api_speed_pl/render.py`` (which the known-good ``bg.ixncfg``
matches field-for-field):

    flow i (global ENI index g = dpu_index*32 + i):
        vlan   = VLAN_OUT_BASE + g
        eth.src = MAC_L_START + g*MAC_STEP_ENI       (== ENI mac_address; NASA
                                                      matches outbound ENI on inner src)
        eth.dst = MAC_R_START + g*MAC_STEP_ENI       (VM gateway MAC; not matched)
        ipv4.src = IP_L_START                        (constant)
        ipv4.dst = IP_R_START + g*IP_STEP_ENI         (counter)
        udp 10000/10000, 128B fixed, fixed 9999-frame burst @1000 fps

Flows are tracked by VLAN id, so the Flow Statistics view yields one row per ENI
(with First TimeStamp = when that ENI first forwarded in hardware).

The traffic loops IxNetwork -> UHD (VLAN→VXLAN encap) -> DPU -> back; the UHD
must be configured (smartswitch-nvidia.http) so the chassis links come up.

Usage::

    python dash_traffic_ixn_build.py --dpu 0 --build-only      # build + generate, no run
    python dash_traffic_ixn_build.py --dpu 0 --run             # also apply, start, poll stats

This module also exposes build_outbound_config()/start_and_monitor() so the
pytest test can import and drive it instead of loading a saved config.
"""
import argparse
import ipaddress
import logging
import time

logger = logging.getLogger(__name__)

try:
    from ixnetwork_restpy import SessionAssistant
except ImportError:  # pragma: no cover
    SessionAssistant = None

# ════════════════════════════════════════════════════════════════════════════
#  CONFIG  —  EDIT FOR YOUR TESTBED
# ════════════════════════════════════════════════════════════════════════════
IXIA_API_SERVER_IP = "10.36.78.95"
IXIA_API_SERVER_PORT = 11009
IXIA_API_USER = None
IXIA_API_PASSWORD = None

# Chassis + the port wired (through the UHD) to the DPU under test. In bg.ixncfg
# the ports are VTEP_01..16 on 10.36.77.138 cards 7/8; pick the one that carries
# this DPU's outbound VLANs. Loopback: the same port sends and receives.
IXIA_CHASSIS_IP = "10.36.77.138"
TX_PORT_CARD = 7
TX_PORT_PORT = 5          # 10.36.77.138:7:5 == VTEP_09 (DPU0-Out source in bg.ixncfg)

ENIS_PER_DPU = 32
VLAN_OUT_BASE = 1001      # outbound VLAN = VLAN_OUT_BASE + global_eni_index
# Inbound (service->VM). The UHD bridges VLANs 1..32 on the "s" ports to NVGRE
# toward the DPU (VLAN 1001.. on the "c" ports are the outbound VXLAN). The
# inbound source is IxNetwork port VTEP_01 = chassis 10.36.77.138 card7 port1
# (from bg.scriptgen.tcl -location list; VTEP_09 == 7;5 confirms the order).
VLAN_IN_BASE = 1          # inbound VLAN = VLAN_IN_BASE + global_eni_index
RX_PORT_CARD = 7          # IxNetwork port (VTEP_01) wired to the UHD inbound ("s") port
RX_PORT_PORT = 1

# Per-flow packet template — matches render.py DEFAULTS / bg.ixncfg exactly.
MAC_L_START = "00:1a:c5:00:00:01"     # eth.src (constant, VM side)
MAC_R_START = "00:1b:6e:00:00:01"     # eth.dst base (per-ENI: + g*MAC_STEP_ENI)
MAC_STEP_ENI = "00:00:00:18:00:00"
IP_L_START = "1.1.0.1"                # ipv4.src (constant)
IP_R_START = "1.4.0.1"                # ipv4.dst base (per-ENI: + g*IP_STEP_ENI)
IP_STEP_ENI = "0.64.0.0"
UDP_SRC_PORT = 10000
UDP_DST_PORT = 10000
# Inbound (service->VM) inner L4 ports — the EXACT 5-tuple reverse of the outbound
# CA flow so the return matches the reverse flow the outbound burst creates on the
# DPU (DASH private-link return is flow-based, not slow-path inbound-routing). The
# outbound CA UDP is 10000->10000, so the reverse is 10000->10000 (swap = same).
INBOUND_UDP_SPORT = UDP_DST_PORT
INBOUND_UDP_DPORT = UDP_SRC_PORT
FRAME_SIZE = 128
# Gentle trickle. The DPU/NASA dataplane handles ~20 Mpps; 200 fps is ~100,000x
# below that, so a packet drop here cannot be rate/overflow induced. (A line-rate
# blast would also be invisible to the per-direction counts since the burst is a
# fixed frame count, not continuous — but keeping it slow removes all doubt.)
PER_FLOW_RATE_FPS = 200
# Fixed (non-continuous) burst sizes so per-run counts are deterministic and the
# direction that fails is obvious from the raw count. OUTBOUND = 9999, INBOUND =
# 7777 (inbound builder lands in Phase 4; the constant is ready for it).
OUTBOUND_FRAME_COUNT = 9999
INBOUND_FRAME_COUNT = 7777

# Layer-1 settings — must match the UHD ixnetwork-facing ports (manual_RS @ 100G).
# Verified against bg.ixncfg: novusHundredGigLan / speed100g / autoneg off / IEEE
# defaults (RS-FEC) -> all 16 chassis ports link up against the configured UHD.
L1_SPEED = "speed100g"
L1_AUTONEG = False
L1_IEEE_DEFAULTS = True
# ════════════════════════════════════════════════════════════════════════════


def _mac_to_int(m):
    return int(m.replace(":", ""), 16)


def _int_to_mac(n):
    h = "%012x" % (n & 0xFFFFFFFFFFFF)
    return ":".join(h[i:i + 2] for i in range(0, 12, 2))


def _ip_to_int(s):
    return int(ipaddress.ip_address(s))


def _int_to_ip(n):
    return str(ipaddress.ip_address(n & 0xFFFFFFFF))


def eni_flow_params(dpu_index, enis_per_dpu=ENIS_PER_DPU):
    """Yield (global_index, vlan, eth_dst_base, ip_dst_base) for each ENI of a DPU.

    eth_dst/ip_dst here are the *base* (first-route) values per ENI — the same
    values the UHD's connect_vlan_vxlan routing keys on.
    """
    mac_r0 = _mac_to_int(MAC_R_START)
    mac_step = _mac_to_int(MAC_STEP_ENI)
    ip_r0 = _ip_to_int(IP_R_START)
    ip_step = _ip_to_int(IP_STEP_ENI)
    for i in range(enis_per_dpu):
        g = dpu_index * enis_per_dpu + i
        yield (
            g,
            VLAN_OUT_BASE + g,
            _int_to_mac(mac_r0 + g * mac_step),
            _int_to_ip(ip_r0 + g * ip_step),
        )


def _connect():
    assert SessionAssistant is not None, "ixnetwork_restpy not installed"
    kwargs = dict(IpAddress=IXIA_API_SERVER_IP, RestPort=IXIA_API_SERVER_PORT,
                  SessionName="dash-build", ClearConfig=True, LogLevel="info")
    if IXIA_API_USER:
        kwargs["UserName"] = IXIA_API_USER
    if IXIA_API_PASSWORD:
        kwargs["Password"] = IXIA_API_PASSWORD
    sa = SessionAssistant(**kwargs)
    return sa, sa.Ixnetwork


def _set_field(stack, field_id_substr, *, single=None, start=None, step=None, count=None):
    """Set a raw-traffic field as a singleValue or an increment (per-ENI counter).

    Raw-traffic varying fields use ValueType='increment' (NOT 'counter', which is
    silently ignored and leaves the field at its singleValue default).
    """
    for f in stack.Field.find():
        if field_id_substr in f.FieldTypeId:
            if single is not None:
                f.update(ValueType="singleValue", SingleValue=str(single), Auto=False)
            else:
                f.update(ValueType="increment", StartValue=str(start),
                         StepValue=str(step), CountValue=int(count), Auto=False)
            return f
    raise AssertionError(f"field '{field_id_substr}' not on stack {stack.StackTypeId}")


def _configure_l1(vport):
    """Set the vport's L1 (speed/FEC/autoneg) to match the UHD ixnetwork ports.

    Must run after AssignPorts (the L1 sub-type is only known once the port type
    is resolved). Tolerant of card types that lack a given attribute.
    """
    l1 = vport.L1Config
    ct = l1.CurrentType
    if not ct:
        logger.warning("L1: vport %s has no resolved type yet; skipping L1 config", vport.Name)
        return
    obj = getattr(l1, ct[0].upper() + ct[1:], None)
    if obj is None:
        logger.warning("L1: no L1 node for type %s; skipping", ct)
        return
    for attr, val in (("Speed", L1_SPEED),
                      ("EnableAutoNegotiation", L1_AUTONEG),
                      ("IeeeL1Defaults", L1_IEEE_DEFAULTS)):
        if hasattr(obj, attr):
            try:
                setattr(obj, attr, val)
            except Exception:
                logger.exception("L1: failed to set %s=%s on %s", attr, val, ct)
    logger.info("L1: %s -> type=%s speed=%s autoneg=%s", vport.Name, ct, L1_SPEED, L1_AUTONEG)


def assign_dual_ports(ixnetwork):
    """Create + assign the two shared IxNetwork ports for a TX/RX-split test:
      vp_b = chassis TX_PORT (7:5) -> UHD ixnetwork_port_1B (VLAN 1001 / VXLAN, outbound)
      vp_a = chassis RX_PORT (7:1) -> UHD ixnetwork_port_1A (VLAN 1   / NVGRE, inbound)
    Outbound TX on vp_b / RX on vp_a; inbound TX on vp_a / RX on vp_b. Returns (vp_b, vp_a)."""
    vp_b = ixnetwork.Vport.add(Name="VTEP_B_%d_%d" % (TX_PORT_CARD, TX_PORT_PORT))
    vp_a = ixnetwork.Vport.add(Name="VTEP_A_%d_%d" % (RX_PORT_CARD, RX_PORT_PORT))
    ixnetwork.AssignPorts(
        [{"Arg1": IXIA_CHASSIS_IP, "Arg2": TX_PORT_CARD, "Arg3": TX_PORT_PORT},
         {"Arg1": IXIA_CHASSIS_IP, "Arg2": RX_PORT_CARD, "Arg3": RX_PORT_PORT}],
        [], [vp_b.href, vp_a.href], True,
    )
    _configure_l1(vp_b)
    _configure_l1(vp_a)
    return vp_b, vp_a


def build_outbound_config(ixnetwork, dpu_index, vp_tx, vp_rx, enis_per_dpu=ENIS_PER_DPU,
                          frame_count=OUTBOUND_FRAME_COUNT, continuous=False):
    """Build (from a cleared config) the per-ENI outbound traffic item for one DPU.

    Returns the TrafficItem. TWO ports (no loopback): transmits on ``vp_tx``
    (chassis 7:5 -> UHD ixnetwork_port_1B, VLAN 1001/VXLAN) and receives the DPU's
    return on ``vp_rx`` (chassis 7:1 -> UHD ixnetwork_port_1A). So the TX port
    counts ONLY outbound (frame_count, default 9999) and the RX port counts only
    the return. ``enis_per_dpu`` ENI flows via counters (vlan / eth.dst / ipv4.dst),
    tracked by VLAN.

    ``continuous=True`` runs the traffic non-stop (instead of a fixed burst) so it
    spans the whole gNMI programming window: each per-VLAN flow's "First TimeStamp"
    then records exactly when that ENI first started forwarding in hardware. Stop it
    explicitly with ``StopStatelessTrafficBlocking``. ``continuous=False`` keeps the
    deterministic fixed-count burst.
    """
    # one raw traffic item, eth/vlan/ipv4/udp stack; source = TX port, dest = RX port.
    ti = ixnetwork.Traffic.TrafficItem.add(
        Name=f"DPU{dpu_index}-Out", TrafficType="raw", BiDirectional=False)
    ti.EndpointSet.add(Sources=vp_tx.Protocols.find(), Destinations=vp_rx.Protocols.find())
    ce = ti.ConfigElement.find()[0]
    ce.FrameSize.update(Type="fixed", FixedSize=FRAME_SIZE)
    ce.FrameRate.update(Type="framesPerSecond", Rate=PER_FLOW_RATE_FPS)
    if continuous:
        # Run non-stop across the programming window so per-VLAN First TimeStamp
        # captures each ENI's hardware bring-up moment.
        ce.TransmissionControl.update(Type="continuous")
    else:
        # Fixed burst (deterministic counts). FrameCount is the total for this config
        # element; with 1 ENI flow that is exactly frame_count.
        ce.TransmissionControl.update(Type="fixedFrameCount", FrameCount=frame_count)

    eth = ce.Stack.find(StackTypeId="^ethernet$")[0]
    vlan = ce.Stack.read(eth.AppendProtocol(
        ixnetwork.Traffic.ProtocolTemplate.find(StackTypeId="^vlan$")))
    ipv4 = ce.Stack.read(vlan.AppendProtocol(
        ixnetwork.Traffic.ProtocolTemplate.find(StackTypeId="^ipv4$")))
    udp = ce.Stack.read(ipv4.AppendProtocol(
        ixnetwork.Traffic.ProtocolTemplate.find(StackTypeId="^udp$")))

    # 3. per-ENI counters (global offset folds dpu_index into the start values).
    g0 = dpu_index * enis_per_dpu
    mac_l0 = _mac_to_int(MAC_L_START) + g0 * _mac_to_int(MAC_STEP_ENI)
    mac_r0 = _mac_to_int(MAC_R_START) + g0 * _mac_to_int(MAC_STEP_ENI)
    ip_r0 = _ip_to_int(IP_R_START) + g0 * _ip_to_int(IP_STEP_ENI)

    # NASA matches the OUTBOUND ENI on the inner SOURCE MAC, which must equal the
    # ENI's programmed mac_address (render.py: MAC_L_START + g*MAC_STEP_ENI == the
    # VM's own MAC). Verified on DPU0: a packet with inner src = ENI mac passes the
    # ENI lookup (ENI_MISS stays flat); inner dst = ENI mac does NOT. Use per-ENI
    # MAC_L on src so every flow's src matches its own ENI (the old code used a
    # constant src, which only matched ENI index 0). Inner dst = the VM's gateway
    # MAC (MAC_R, per-ENI); not used for ENI lookup.
    _set_field(eth, "ethernet.header.sourceAddress",
               start=_int_to_mac(mac_l0), step=MAC_STEP_ENI, count=enis_per_dpu)
    _set_field(eth, "ethernet.header.destinationAddress",
               start=_int_to_mac(mac_r0), step=MAC_STEP_ENI, count=enis_per_dpu)
    _set_field(vlan, "vlanTag.vlanID",
               start=VLAN_OUT_BASE + g0, step=1, count=enis_per_dpu)
    _set_field(ipv4, "ipv4.header.srcIp", single=IP_L_START)
    _set_field(ipv4, "ipv4.header.dstIp",
               start=_int_to_ip(ip_r0), step=IP_STEP_ENI, count=enis_per_dpu)
    _set_field(udp, "udp.header.srcPort", single=UDP_SRC_PORT)
    _set_field(udp, "udp.header.dstPort", single=UDP_DST_PORT)

    # 4. track per-VLAN -> one Flow Statistics row per ENI.
    ti.Tracking.find().TrackBy = ["trackingenabled0", "vlanVlanId0"]

    ti.Generate()
    ixnetwork.Traffic.Apply()
    logger.info("Built DPU%d-Out: %d ENI flows, VLANs %d..%d",
                dpu_index, enis_per_dpu, VLAN_OUT_BASE + g0,
                VLAN_OUT_BASE + g0 + enis_per_dpu - 1)
    return ti


def _ipv4_to_overlay_v6(ipv4, eni):
    """IPv6 'corresponding to' an IPv4, per map.json.j2 overlay_dip_prefix
    (2603:100:<eni_hex>:0::<hi16>:<lo16>). e.g. 1.4.0.1, eni 1000 ->
    2603:100:3e8:0::104:1 ; 1.1.0.1 -> 2603:100:3e8:0::101:1."""
    n = _ip_to_int(ipv4)
    return "2603:100:%x:0::%x:%x" % (eni, (n >> 16) & 0xFFFF, n & 0xFFFF)


def _vm_pl_overlay_v6(ca_ipv4, eni):
    """The VM's PL-encoded overlay address exactly as the DPU emits it on outbound
    CA2PA (captured ground truth — tests/dash/captures/dpu_outbound_overlay_port2.txt).

    It is the bitwise OR of three pieces:
      pl_sip base       fd40::<vni_le>:64:ff71:0:0   (ENI pl_sip_encoding; vni_le =
                                                      byte-swapped r_vni = eni+1000)
      overlay_sip_prefix 1:100:<eni_hex>::           (map.json.j2)
      CA low 32 bits     ::<hi16>:<lo16>             (the customer IPv4)
    e.g. ca 1.1.0.1, eni 1000 (r_vni 2000) -> fd41:100:3e8:d007:64:ff71:101:1.
    This is what the inbound (service->VM) return must use as the inner DESTINATION;
    the old 2603:.. form (used for the service side) is wrong for the VM side."""
    r_vni = eni + 1000                        # ENI_L2R_STEP (render.py DEFAULTS)
    h = "%04x" % r_vni
    vni_le = h[2:4] + h[0:2]                   # 2000 -> '07d0' -> 'd007'
    base = int(ipaddress.IPv6Address("fd40::%s:64:ff71:0:0" % vni_le))
    osip = int(ipaddress.IPv6Address("1:100:%x::" % eni))
    ca = _ip_to_int(ca_ipv4) & 0xFFFFFFFF     # CA occupies the low 32 bits
    return str(ipaddress.IPv6Address(base | osip | ca))


def build_inbound_config(ixnetwork, dpu_index, vp_tx, vp_rx, enis_per_dpu=ENIS_PER_DPU,
                         frame_count=INBOUND_FRAME_COUNT, eni_start=1000):
    """Build the per-ENI INBOUND (service->VM) traffic item for one DPU.

    Mirror of the outbound TI but: ethernet/vlan/IPv6/UDP stack, inbound VLAN
    base (1..). Inner IPv6 = service overlay (2603:100:<eni>::<v4> of 1.4.0.1) ->
    VM PL-encoded address (fd41:.. of 1.1.0.1), matching the DPU's own overlay
    addressing captured at port2 (dpu_outbound_overlay_port2.txt). NASA matches the
    inbound ENI on the inner DESTINATION MAC (= the ENI mac), the mirror of outbound
    (which matches on inner src). The UHD adds the NVGRE encap.

    The inner UDP header is REQUIRED: NASA runs its protocol/flow stage on the
    DECAPPED inner packet, and a bare IPv6 with no L4 (next-header = none) is
    rejected as SAI_ENI_STAT_UNSUPPORTED_PROTOCOL_DROP *after* a clean inbound
    decap (INBOUND_RX counted, INBOUND_ROUTING/PA_VALIDATION/TRUSTED_VNI all 0
    misses). Appending UDP sets ipv6.nextHeader=17 so the flow is created and the
    packet forwards — verified on DPU0 (keysight-nss01).
    TWO ports (no loopback): transmits on ``vp_tx`` (chassis 7:1 -> UHD
    ixnetwork_port_1A, VLAN 1/NVGRE) and receives the return on ``vp_rx`` (7:5 ->
    1B). NOTE: exact overlay IPv6 inferred from the saved config.
    """
    ti = ixnetwork.Traffic.TrafficItem.add(
        Name=f"DPU{dpu_index}-In", TrafficType="raw", BiDirectional=False)
    ti.EndpointSet.add(Sources=vp_tx.Protocols.find(), Destinations=vp_rx.Protocols.find())
    ce = ti.ConfigElement.find()[0]
    ce.FrameSize.update(Type="fixed", FixedSize=FRAME_SIZE)
    ce.FrameRate.update(Type="framesPerSecond", Rate=PER_FLOW_RATE_FPS)
    ce.TransmissionControl.update(Type="fixedFrameCount", FrameCount=frame_count)

    eth = ce.Stack.find(StackTypeId="^ethernet$")[0]
    vlan = ce.Stack.read(eth.AppendProtocol(
        ixnetwork.Traffic.ProtocolTemplate.find(StackTypeId="^vlan$")))
    ipv6 = ce.Stack.read(vlan.AppendProtocol(
        ixnetwork.Traffic.ProtocolTemplate.find(StackTypeId="^ipv6$")))
    udp = ce.Stack.read(ipv6.AppendProtocol(
        ixnetwork.Traffic.ProtocolTemplate.find(StackTypeId="^udp$")))

    g0 = dpu_index * enis_per_dpu
    eni0 = eni_start + g0
    mac_l0 = _mac_to_int(MAC_L_START) + g0 * _mac_to_int(MAC_STEP_ENI)
    mac_r0 = _mac_to_int(MAC_R_START) + g0 * _mac_to_int(MAC_STEP_ENI)

    # Inbound: inner DST mac = ENI mac (NASA matches the ENI on inner dst here);
    # inner src = the remote/gateway mac.
    _set_field(eth, "ethernet.header.destinationAddress",
               start=_int_to_mac(mac_l0), step=MAC_STEP_ENI, count=enis_per_dpu)
    _set_field(eth, "ethernet.header.sourceAddress",
               start=_int_to_mac(mac_r0), step=MAC_STEP_ENI, count=enis_per_dpu)
    _set_field(vlan, "vlanTag.vlanID",
               start=VLAN_IN_BASE + g0, step=1, count=enis_per_dpu)
    # Inner IPv6: service -> VM, matching the DPU's own overlay addressing (captured
    # at port2). src = service overlay (2603:100:<eni>::<v4> of 1.4.0.1). dst = the
    # VM's PL-encoded address (fd41:..) the DPU uses, NOT the 2603:.. form (the old
    # 2603:..101:1 dst was wrong — see dpu_outbound_overlay_port2.txt). Single value
    # for the g0 ENI (minimal single-flow case).
    inner_src = _ipv4_to_overlay_v6(IP_R_START, eni0)        # service (1.4.0.1)
    inner_dst = _vm_pl_overlay_v6(IP_L_START, eni0)          # VM (1.1.0.1), PL-encoded
    _set_field(ipv6, "ipv6.header.srcIP", single=inner_src)
    _set_field(ipv6, "ipv6.header.dstIP", single=inner_dst)
    # Inner L4: required so NASA sees a supported protocol on the decapped packet
    # (else SAI_ENI_STAT_UNSUPPORTED_PROTOCOL_DROP). Inbound uses 22222/22222.
    _set_field(udp, "udp.header.srcPort", single=INBOUND_UDP_SPORT)
    _set_field(udp, "udp.header.dstPort", single=INBOUND_UDP_DPORT)

    ti.Tracking.find().TrackBy = ["trackingenabled0", "vlanVlanId0"]
    ti.Generate()
    ixnetwork.Traffic.Apply()
    logger.info("Built DPU%d-In: %d ENI flows, VLANs %d..%d, IPv6 %s -> %s, UDP %d->%d",
                dpu_index, enis_per_dpu, VLAN_IN_BASE + g0,
                VLAN_IN_BASE + g0 + enis_per_dpu - 1,
                inner_src, inner_dst, INBOUND_UDP_SPORT, INBOUND_UDP_DPORT)
    return ti


def read_flow_stats(ixnetwork, vlan_base=VLAN_OUT_BASE):
    """Per-ENI Flow Statistics keyed by global ENI index (= vlan - vlan_base)."""
    views = ixnetwork.Statistics.View.find(Caption="Flow Statistics")
    if len(views) == 0:
        return {}
    data = views[0].Data
    caps = list(data.ColumnCaptions)

    def col(*subs):
        for i, c in enumerate(caps):
            if all(s.lower() in c.lower() for s in subs):
                return i
        return None

    ci = {k: col(*v) for k, v in {
        "vlan": ("vlan", "id"), "tx": ("tx frames",), "rx": ("rx frames",),
        "loss": ("loss", "%"), "first": ("first", "timestamp"),
    }.items()}

    def _i(x):
        try:
            return int(float(str(x).replace(",", "")))
        except (ValueError, TypeError):
            return 0

    out = {}
    for page in range(1, (data.TotalPages or 1) + 1):
        if data.CurrentPage != page:
            data.CurrentPage = page
        for raw in data.PageValues:
            cells = raw[0] if (raw and isinstance(raw[0], list)) else raw
            if ci["vlan"] is None:
                continue
            vlan = _i(cells[ci["vlan"]])
            if vlan <= 0:
                continue
            out[vlan - vlan_base] = {
                "vlan": vlan,
                "tx": _i(cells[ci["tx"]]) if ci["tx"] is not None else 0,
                "rx": _i(cells[ci["rx"]]) if ci["rx"] is not None else 0,
                "loss": str(cells[ci["loss"]]) if ci["loss"] is not None else "",
                "first": str(cells[ci["first"]]) if ci["first"] is not None else "",
            }
    return out


def _main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--dpu", type=int, default=0)
    ap.add_argument("--run", action="store_true", help="apply, start traffic, poll stats")
    ap.add_argument("--build-only", action="store_true", help="build+generate only (default)")
    ap.add_argument("--seconds", type=int, default=20, help="run duration to poll stats")
    args = ap.parse_args()

    logger.info("Per-ENI flow plan for DPU%d:", args.dpu)
    for g, vlan, ethd, ipd in eni_flow_params(args.dpu):
        logger.info("  g=%-3d vlan=%-5d eth.dst=%s ip.dst=%s", g, vlan, ethd, ipd)

    sa, ixn = _connect()
    build_outbound_config(ixn, args.dpu)
    states = {vp.Name: vp.State for vp in ixn.Vport.find()}
    logger.info("Port states: %s", states)

    if args.run:
        logger.info("Starting continuous traffic for %ds ...", args.seconds)
        ixn.Traffic.StartStatelessTrafficBlocking()
        deadline = time.time() + args.seconds
        while time.time() < deadline:
            time.sleep(3)
            stats = read_flow_stats(ixn)
            fwd = sum(1 for s in stats.values() if s["first"].strip())
            logger.info("  flows=%d forwarding=%d", len(stats), fwd)
        ixn.Traffic.StopStatelessTrafficBlocking()
        for g in sorted(read_flow_stats(ixn)):
            s = read_flow_stats(ixn)[g]
            logger.info("  ENI %-3d vlan=%d rx=%d loss=%s first=%s",
                        g, s["vlan"], s["rx"], s["loss"], s["first"])


if __name__ == "__main__":
    _main()
