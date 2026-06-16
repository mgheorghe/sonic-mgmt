#!/usr/bin/env python3
"""Build the DASH per-ENI traffic config on IxNetwork *live* via RestPy.

This is the "create the config from scratch" alternative to loading ``bg.ixncfg``.
It programmatically builds, for one DPU, an outbound (VXLAN) raw traffic item
with 32 per-ENI flows — one unique VLAN each — using the exact same arithmetic
as ``configs/dash_api_speed_pl/render.py`` (which the known-good ``bg.ixncfg``
matches field-for-field):

    flow i (global ENI index g = dpu_index*32 + i):
        vlan   = VLAN_OUT_BASE + g
        eth.dst = MAC_L_START + g*MAC_STEP_ENI       (== ENI mac_address; NASA
                                                      matches outbound ENI on inner dst)
        eth.src = MAC_R_START + g*MAC_STEP_ENI       (VM's own MAC; not matched)
        ipv4.src = IP_L_START                        (constant)
        ipv4.dst = IP_R_START + g*IP_STEP_ENI         (counter)
        udp 10000/10000, 128B fixed, continuous 1000 fps

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

# Per-flow packet template — matches render.py DEFAULTS / bg.ixncfg exactly.
MAC_L_START = "00:1a:c5:00:00:01"     # eth.src (constant, VM side)
MAC_R_START = "00:1b:6e:00:00:01"     # eth.dst base (per-ENI: + g*MAC_STEP_ENI)
MAC_STEP_ENI = "00:00:00:18:00:00"
IP_L_START = "1.1.0.1"                # ipv4.src (constant)
IP_R_START = "1.4.0.1"                # ipv4.dst base (per-ENI: + g*IP_STEP_ENI)
IP_STEP_ENI = "0.64.0.0"
UDP_SRC_PORT = 10000
UDP_DST_PORT = 10000
FRAME_SIZE = 128
PER_FLOW_RATE_FPS = 1000

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


def build_outbound_config(ixnetwork, dpu_index, enis_per_dpu=ENIS_PER_DPU):
    """Build (from a cleared config) the per-ENI outbound traffic item for one DPU.

    Returns the TrafficItem. One raw TI, single tx/rx port (loopback through the
    UHD), 32 ENI flows via counters (vlan / eth.dst / ipv4.dst), tracked by VLAN.
    """
    # 1. one vport on the DPU's chassis port (sends and receives the loop).
    vport = ixnetwork.Vport.add(Name=f"VTEP_dpu{dpu_index}")
    ixnetwork.AssignPorts(
        [{"Arg1": IXIA_CHASSIS_IP, "Arg2": TX_PORT_CARD, "Arg3": TX_PORT_PORT}],
        [], [vport.href], True,
    )
    _configure_l1(vport)

    # 2. one raw traffic item, eth/vlan/ipv4/udp stack.
    ti = ixnetwork.Traffic.TrafficItem.add(
        Name=f"DPU{dpu_index}-Out", TrafficType="raw", BiDirectional=False)
    ti.EndpointSet.add(Sources=vport.Protocols.find(), Destinations=vport.Protocols.find())
    ce = ti.ConfigElement.find()[0]
    ce.FrameSize.update(Type="fixed", FixedSize=FRAME_SIZE)
    ce.FrameRate.update(Type="framesPerSecond", Rate=PER_FLOW_RATE_FPS)
    ce.TransmissionControl.update(Type="continuous")

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

    # NASA matches the OUTBOUND ENI on the inner DESTINATION MAC, which must equal
    # the ENI's programmed mac_address (render.py: MAC_L_START + g*MAC_STEP_ENI).
    # The inner SOURCE MAC is the VM's own MAC and is not used for ENI lookup.
    # (Previously src=MAC_L const / dst=MAC_R -> inner dst never matched the ENI mac
    # -> ENI_MISS for every flow even though VIP/direction were correct.)
    _set_field(eth, "ethernet.header.destinationAddress",
               start=_int_to_mac(mac_l0), step=MAC_STEP_ENI, count=enis_per_dpu)
    _set_field(eth, "ethernet.header.sourceAddress",
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
