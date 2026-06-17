"""Shared helpers for the DASH API load-speed tests.

Both ``test_dash_api_speed_pl.py`` (gRPC-only timing) and
``test_dash_api_speed_pl_with_traffic.py`` (gRPC vs. live-traffic timing)
import from here so the config-push path is identical between them.

The push (:func:`load_json_via_gnmi`) optionally records a per-file
*push event* — start/end wall-clock and the parsed ENI index/kind — into a
caller-supplied ``push_events`` dict, so the traffic test can correlate when
each ENI's config finished pushing (the "gRPC time") against when its flow
started forwarding in hardware (the "traffic time").
"""
import json
import logging
import os
import re
import shlex
import time

import pytest
from gnmi_utils import GNMIEnvironment

logger = logging.getLogger(__name__)

# gnmi-agent container image
_GNMI_AGENT_IMAGE = "sonic-gnmi-agent:2026march13"

_GNMI_CONTAINER_NAME = "sonic-gnmi-agent-push"

# Native gNMI push (works against the NPU's `telemetry --noTLS` plaintext server).
# The extracted client opens a grpc.insecure_channel and sends user/pass as
# metadata; the docker gnmi_set CLI cannot (it TLS-handshakes / refuses creds
# over plaintext, silently no-ops, leaving DPU_APPL_DB empty).
_GNMI_EXTRACTED_SUBDIR = "gnmi_agent_extracted"
_BATCH_VAL = 3000  # pl_100 sweet spot (see reference_dash_perf_facts)

# TEMP experiment: push apl+eni files (create VNET/ENI) first, barrier until the
# VNETs are programmed into ASIC_DB, then push map files (mappings). Works around
# the DPU orchagent issuing OUTBOUND_CA_TO_PA mappings before their VNET exists.
_PHASED_PUSH = False

_NPU_STATIC_ARP = [
    ("220.0.1.2", "80:09:02:02:00:01"),
    ("220.0.2.2", "80:09:02:02:00:02"),
    ("220.0.3.2", "80:09:02:02:00:03"),
    ("220.0.4.2", "80:09:02:02:00:04"),
]

# Per-port PA return routes — keep the two traffic directions separable on the
# two NPU<->UHD links so per-interface counters trace each direction cleanly:
#   port1 = Ethernet0 (220.0.1.x) = outbound / VXLAN / 221.1.0.0 (vni 1000)
#   port2 = Ethernet8 (220.0.2.x) = inbound  / NVGRE / 221.2.0.0 (vsid 100)
# The optimized UHD config (smartswitch-nvidia-optimized.http) bridges
# dpu_port_1<->Ethernet0 (221.1) and dpu_port_2<->Ethernet8 (221.2). Without
# this split both supernets resolve via Ethernet0 and the diagram can't tell the
# directions apart.  (nexthop must have a matching _NPU_STATIC_ARP entry.)
_NPU_RETURN_ROUTES = [
    ("221.1.0.0/16", "220.0.1.2"),   # outbound -> port1 / Ethernet0
    ("221.2.0.0/16", "220.0.2.2"),   # inbound  -> port2 / Ethernet8
]
# Stale more-specifics from earlier single-/split-port attempts; a /26 beats the
# /16 above, so remove any that point the wrong direction before adding the /16s.
_NPU_STALE_ROUTES = [
    ("221.1.0.0/26", "220.0.1.2"), ("221.1.0.64/26", "220.0.2.2"),
    ("221.2.0.0/26", "220.0.1.2"), ("221.2.0.64/26", "220.0.2.2"),
]

# Filename pattern: pl_100.dpu0.001eni.json -> index 001, kind "eni".
_FILE_INDEX_RE = re.compile(r"\.(\d{3})(apl|eni|map)\.json$")


def parse_file_index(filename):
    """Return (index:int, kind:str) for a rendered config file, or (None, None)."""
    m = _FILE_INDEX_RE.search(filename)
    if not m:
        return None, None
    return int(m.group(1)), m.group(2)


def _parse_mem_str(mem_str):
    """Parse a docker memory string like '512MiB', '1.5GiB', '256kB' into MiB."""
    m = re.match(r"([\d.]+)\s*(B|kB|MiB|GiB|TiB)", mem_str.strip())
    if not m:
        return 0.0
    val = float(m.group(1))
    unit = m.group(2)
    if unit == "B":
        return val / (1024 * 1024)
    if unit == "kB":
        return val / 1024
    if unit == "MiB":
        return val
    if unit == "GiB":
        return val * 1024
    if unit == "TiB":
        return val * 1024 * 1024
    return val


def _collect_memory(host):
    """Per-container memory (MiB) plus '_system_*' keys from `free -m`."""
    result = {}

    # awk avoids Jinja2 issues with Go's {{.Name}}; docker stats cols: ID NAME CPU% MEM_USED / ...
    out = host.shell("docker stats --no-stream | awk 'NR>1 {print $2\"\\t\"$4}'", module_ignore_errors=True)
    for line in out.get("stdout", "").splitlines():
        line = line.strip()
        if not line or "\t" not in line:
            continue
        name, used_str = line.split("\t", 1)
        result[name.strip()] = _parse_mem_str(used_str.strip())

    result.update(_collect_free_memory(host))
    return result


def _collect_free_memory(host):
    """Run ``free -m`` on *host* and return a dict with system memory keys (MiB)."""
    result = {}
    free_out = host.shell("free -m", module_ignore_errors=True)
    for line in free_out.get("stdout", "").splitlines():
        if line.startswith("Mem:"):
            parts = line.split()
            # free -m columns: total used free shared buff/cache available
            result["_system_total"] = float(parts[1])
            result["_system_used"] = float(parts[2])
            result["_system_free"] = float(parts[3])
            if len(parts) >= 7:
                result["_system_available"] = float(parts[6])
    return result


def _collect_redis_memory(dpuhost):
    """Redis memory info from DPU_APPL_DB: totals plus 2 VNET_MAPPING key samples."""
    result = {}

    info_out = dpuhost.shell("sonic-db-cli DPU_APPL_DB INFO MEMORY", module_ignore_errors=True)
    for line in info_out.get("stdout", "").splitlines():
        line = line.strip()
        if line.startswith("used_memory:"):
            try:
                result["_used_memory"] = int(line.split(":")[1])
            except ValueError:
                pass
        elif line.startswith("used_memory_human:"):
            result["_used_memory_human"] = line.split(":", 1)[1].strip()

    keys_out = dpuhost.shell(
        "sonic-db-cli DPU_APPL_DB KEYS 'DASH_VNET_MAPPING_TABLE:*' 2>/dev/null | head -2",
        module_ignore_errors=True)
    for key in keys_out.get("stdout", "").splitlines():
        key = key.strip()
        if not key:
            continue
        usage_out = dpuhost.shell(f"sonic-db-cli DPU_APPL_DB MEMORY USAGE '{key}'", module_ignore_errors=True)
        try:
            result[key] = int(usage_out.get("stdout", "0").strip())
        except ValueError:
            result[key] = 0

    return result


def _print_results(timings, total_elapsed, mem_before, mem_after,
                   redis_before, redis_after, mem_timeline=None):
    sep = "=" * 72
    logger.info(sep)
    logger.info("  DASH API LOAD SPEED TEST — RESULTS")
    logger.info(sep)

    logger.info("\n  Per-file load times:")
    logger.info("  %-44s  %8s", "File", "Time (s)")
    logger.info("  " + "-" * 56)
    for filename, elapsed in timings.items():
        logger.info("  %-44s  %8.2f", filename, elapsed)
    logger.info("  " + "-" * 56)
    logger.info("  %-44s  %8.2f", "TOTAL", total_elapsed)
    if timings:
        logger.info("  %-44s  %8.2f", "Average per file", total_elapsed / len(timings))
    logger.info("  Files loaded: %d", len(timings))

    for host_label in ("NPU", "DPU"):
        before = mem_before[host_label]
        after = mem_after[host_label]

        all_containers = sorted(
            k for k in set(before) | set(after) if not k.startswith("_")
        )

        logger.info("\n  Memory usage — %s (MiB):", host_label)
        logger.info("  %-30s  %8s  %8s  %8s", "Container", "Before", "After", "Delta")
        logger.info("  " + "-" * 58)

        total_before = 0.0
        total_after = 0.0
        for name in all_containers:
            b = before.get(name, 0.0)
            a = after.get(name, 0.0)
            total_before += b
            total_after += a
            logger.info("  %-30s  %8.1f  %8.1f  %+8.1f", name, b, a, a - b)

        logger.info("  " + "-" * 58)
        logger.info("  %-30s  %8.1f  %8.1f  %+8.1f", "Containers total",
                    total_before, total_after, total_after - total_before)

        sys_b = before.get("_system_used", 0.0)
        sys_a = after.get("_system_used", 0.0)
        logger.info("  %-30s  %8.1f  %8.1f  %+8.1f", "System used (free -m)", sys_b, sys_a, sys_a - sys_b)
        sys_total = before.get("_system_total", after.get("_system_total", 0.0))
        for key, label in [("_system_free", "System free"), ("_system_available", "System available")]:
            b = before.get(key, 0.0)
            a = after.get(key, 0.0)
            logger.info("  %-30s  %8.1f  %8.1f  %+8.1f", label, b, a, a - b)
        if sys_total:
            logger.info("  %-30s  %8.1f", "System total", sys_total)

    # Memory timeline (per-file free memory after each push)
    if mem_timeline:
        logger.info("\n  Memory timeline — free memory after each file push (MiB):")
        logger.info("  %-6s  %-40s  %7s  %9s  %9s  %9s  %9s",
                    "#", "File", "Ops", "NPU free", "NPU avail", "DPU free", "DPU avail")
        logger.info("  " + "-" * 96)
        for entry in mem_timeline:
            logger.info("  %-6s  %-40s  %7d  %9.0f  %9.0f  %9.0f  %9.0f",
                        entry["idx"], entry["file"][:40], entry["ops"],
                        entry["npu_free"], entry["npu_available"],
                        entry["dpu_free"], entry["dpu_available"])
        # Summary: min free across all snapshots
        if len(mem_timeline) > 1:
            logger.info("  " + "-" * 96)
            logger.info("  %-6s  %-40s  %7s  %9.0f  %9.0f  %9.0f  %9.0f", "", "MINIMUM", "",
                        min(e["npu_free"] for e in mem_timeline),
                        min(e["npu_available"] for e in mem_timeline),
                        min(e["dpu_free"] for e in mem_timeline),
                        min(e["dpu_available"] for e in mem_timeline))

    # Redis (DPU_APPL_DB) memory
    logger.info("\n  DPU Redis memory — DPU_APPL_DB (bytes):")
    logger.info("  %-52s  %10s  %10s  %10s", "Key", "Before", "After", "Delta")
    logger.info("  " + "-" * 86)

    redis_b_total = redis_before.get("_used_memory", 0)
    redis_a_total = redis_after.get("_used_memory", 0)
    logger.info("  %-52s  %10d  %10d  %+10d", "used_memory (total)",
                redis_b_total, redis_a_total, redis_a_total - redis_b_total)
    logger.info("  %-52s  %10s  %10s", "used_memory_human",
                redis_before.get("_used_memory_human", "n/a"),
                redis_after.get("_used_memory_human", "n/a"))

    sample_keys = sorted(k for k in set(redis_before) | set(redis_after) if not k.startswith("_"))
    for key in sample_keys:
        b = redis_before.get(key, 0)
        a = redis_after.get(key, 0)
        logger.info("  %-52s  %10d  %10d  %+10d", key, b, a, a - b)

    logger.info(sep)


def npu_pre_config(duthost, dpu_midplane_ip, dpu_dataplane_ip):
    """Prepare NPU for DASH push: static ARPs, midplane/dataplane ping, log routes/ifaces."""
    logger.info("NPU: adding permanent static ARP entries for dataplane next-hops")
    for ip, mac in _NPU_STATIC_ARP:
        route_out = duthost.shell(f"ip route get {ip}", module_ignore_errors=True)
        tokens = route_out.get("stdout", "").split()
        dev = next((tokens[i + 1] for i, t in enumerate(tokens) if t == "dev"), None)
        assert dev, f"Could not determine egress interface for {ip} on NPU"

        for attempt in range(3):
            duthost.shell(f"sudo ip neigh replace {ip} lladdr {mac} dev {dev} nud permanent", module_ignore_errors=True)
            verify = duthost.shell(f"ip neigh show {ip}", module_ignore_errors=True)
            if "PERMANENT" in verify.get("stdout", "").upper():
                logger.info("  NPU: permanent ARP %s lladdr %s dev %s (attempt %d)", ip, mac, dev, attempt + 1)
                break
        else:
            raise AssertionError(
                f"Failed to add permanent ARP entry for {ip} after 3 attempts. "
                f"'ip neigh show {ip}': {verify.get('stdout', '')}"
            )

    # Per-port PA return routes: drop stale wrong-way more-specifics, then install
    # the /16 split so 221.1 -> port1/Ethernet0 and 221.2 -> port2/Ethernet8.
    logger.info("NPU: removing stale PA more-specific routes")
    for prefix, nexthop in _NPU_STALE_ROUTES:
        duthost.shell("sudo config route del prefix %s nexthop %s" % (prefix, nexthop),
                      module_ignore_errors=True)
    logger.info("NPU: installing per-port PA return routes (221.1->Eth0, 221.2->Eth8)")
    for prefix, nexthop in _NPU_RETURN_ROUTES:
        duthost.shell("sudo config route add prefix %s nexthop %s" % (prefix, nexthop),
                      module_ignore_errors=True)
    for probe, want in (("221.1.0.1", "220.0.1.2"), ("221.2.0.1", "220.0.2.2")):
        rg = duthost.shell("ip route get %s" % probe, module_ignore_errors=True).get("stdout", "")
        rg1 = rg.splitlines()[0] if rg.strip() else "(no route)"
        if want in rg:
            logger.info("  NPU: PA %s routes via %s (correct port): %s", probe, want, rg1)
        else:
            logger.warning("  NPU: PA %s NOT via %s -> direction not isolated on its port: %s",
                           probe, want, rg1)

    logger.info("NPU: pinging DPU midplane IP %s to populate ARP", dpu_midplane_ip)
    duthost.shell(f"ping -c 3 -W 2 {dpu_midplane_ip}", module_ignore_errors=True)

    arp_out = duthost.shell(f"ip n show {dpu_midplane_ip}", module_ignore_errors=True)
    logger.info("NPU ARP entry for %s: %s", dpu_midplane_ip, arp_out.get("stdout", "").strip() or "(none)")

    logger.info("NPU: show ip route")
    npu_route = duthost.shell("show ip route", module_ignore_errors=True)
    for line in npu_route.get("stdout", "").splitlines():
        logger.info("  NPU route: %s", line)

    logger.info("NPU: show ip interfaces")
    npu_ifaces = duthost.shell("show ip interfaces", module_ignore_errors=True)
    for line in npu_ifaces.get("stdout", "").splitlines():
        logger.info("  NPU iface: %s", line)

    logger.info("NPU: pinging DPU dataplane IP %s", dpu_dataplane_ip)
    ping_out = duthost.shell(f"ping -c 5 -W 2 {dpu_dataplane_ip}", module_ignore_errors=True)
    for line in ping_out.get("stdout", "").splitlines():
        logger.info("  %s", line)


def dpu_pre_config(dpuhost, dpu_dataplane_ip):
    """Prepare DPU dataplane before pushing DASH config (mirrors private-link-nvidia/dpu.py).

    Always done, in this order, before any JSON is loaded:
      1. Ethernet0 = <dataplane_ip>/31
      2. Default route OFF the midplane (169.254.200.254 DHCP route) and ONTO the
         dataplane nexthop (the even /31 peer) via SONiC `config route add`. The
         midplane default is a kernel/DHCP route (not in CONFIG_DB) so it must be
         removed with `ip route del` — the only linux command here, as in dpu.py.
      3. Loopback0 = 221.0.0.<idx+1>/32  (+ verify)
      4. Loopback1 = 221.0.<idx+1>.<idx+1>/32
      5. SWITCH_TABLE:switch vxlan_port = 4789 (+ verify/retry)
      6. PA return routes 221.{1,2,4}.0.0/16 -> dataplane nexthop, then VERIFY each
         supernet resolves out the dataplane (not eth0-midplane) via `ip route get`.
      7. PERMANENT static neighbor for the dataplane nexthop so NASA/DPDK has the
         next-hop MAC to build the NVGRE return packet's outer Ethernet.
    """
    idx = dpuhost.dpu_index
    eth0_cidr = "%s/31" % dpu_dataplane_ip
    octs = dpu_dataplane_ip.split(".")
    nexthop = ".".join(octs[:3] + [str(int(octs[3]) - 1)])  # .57 -> .56 (NPU side)
    midplane_gw = "169.254.200.254"
    loopback0 = "221.0.0.%d/32" % (idx + 1)
    loopback1 = "221.0.%d.%d/32" % (idx + 1, idx + 1)

    logger.info("DPU: adding Ethernet0 IP %s (if not present)", eth0_cidr)
    dpuhost.shell("sudo config interface ip add Ethernet0 %s" % eth0_cidr, module_ignore_errors=True)

    # Default route: swap from midplane to dataplane nexthop (only if not already selected).
    route_out = dpuhost.shell("show ip route", module_ignore_errors=True).get("stdout", "")
    if "S>*0.0.0.0/0" not in route_out:
        logger.info("DPU: removing midplane default (via %s) and adding dataplane default (nexthop %s)",
                    midplane_gw, nexthop)
        dpuhost.shell("sudo ip route del 0.0.0.0/0 via %s" % midplane_gw, module_ignore_errors=True)
        dpuhost.shell("sudo config route add prefix 0.0.0.0/0 nexthop %s" % nexthop, module_ignore_errors=True)
    else:
        logger.info("DPU: dataplane default route already selected, leaving as-is")

    logger.info("DPU: creating Loopback0 interface (if not present)")
    dpuhost.shell("sudo config loopback add Loopback0", module_ignore_errors=True)
    logger.info("DPU: adding Loopback0 IP %s", loopback0)
    dpuhost.shell("sudo config interface ip add Loopback0 %s" % loopback0, module_ignore_errors=True)
    logger.info("DPU: adding Loopback1 IP %s", loopback1)
    dpuhost.shell("sudo config interface ip add Loopback1 %s" % loopback1, module_ignore_errors=True)
    iface_out = dpuhost.shell("show ip interfaces")
    assert "221.0.0.%d" % (idx + 1) in iface_out.get("stdout", ""), \
        "Loopback0 IP %s was not found in 'show ip interfaces' after config" % loopback0

    # VXLAN UDP dport MUST match the traffic generator (UHD/IxNetwork encaps to 4789).
    # A fresh DPU defaults to 65330, which NASA does not recognize -> all VXLAN traffic
    # falls through to the kernel and never reaches the DASH VIP pipeline (100% loss,
    # VIP_MISS never even has a chance). Pin SWITCH_TABLE:switch vxlan_port to 4789.
    # swssconfig is known not to always take on the first apply, so verify + retry.
    vxlan_port_cfg = [{"SWITCH_TABLE:switch": {"vxlan_port": "4789"}, "OP": "SET"}]
    dpuhost.copy(content=json.dumps(vxlan_port_cfg, indent=4),
                 dest="/tmp/vxlan_port_config.json", verbose=False)
    for attempt in range(3):
        logger.info("DPU: setting VXLAN UDP dport to 4789 (attempt %d)", attempt + 1)
        dpuhost.shell("docker cp /tmp/vxlan_port_config.json swss:/vxlan_port_config.json",
                      module_ignore_errors=True)
        dpuhost.shell("docker exec swss sh -c 'swssconfig /vxlan_port_config.json'",
                      module_ignore_errors=True)
        time.sleep(3)
        vxp = dpuhost.shell("redis-cli -n 0 hget SWITCH_TABLE:switch vxlan_port",
                            module_ignore_errors=True).get("stdout", "").strip()
        logger.info("DPU: SWITCH_TABLE:switch vxlan_port now = %s", vxp)
        if vxp == "4789":
            break
    else:
        logger.warning("DPU: vxlan_port did not settle to 4789 (got %s) -- traffic may not "
                       "be recognized by NASA", vxp)

    # Underlay PA RETURN routes (outbound forwarding path). After the DASH CA2PA stage
    # the outbound packet is re-encapped to the remote PA (PAR 221.2.0.0/.., and the
    # VM-side PAL 221.1.0.0/..) and must egress via the DATAPLANE nexthop so it reaches
    # the NPU -> UHD -> IxNetwork. On a fresh DPU the only default is the DHCP midplane
    # route (0.0.0.0/0 via 169.254.200.254 dev eth0-midplane); the midplane is a mgmt
    # interface NASA cannot forward on, so the return is black-holed -> 100% loss even
    # though VIP/direction/ENI/CA2PA are all correct. The default-route swap above is
    # repeatedly reclaimed by the midplane DHCP client, so pin the PA supernets to the
    # dataplane nexthop explicitly (a more-specific route beats the DHCP default and
    # survives lease renewals).
    for pa_supernet in ("221.1.0.0/16", "221.2.0.0/16", "221.4.0.0/16"):
        logger.info("DPU: adding underlay PA return route %s -> %s", pa_supernet, nexthop)
        dpuhost.shell("sudo config route add prefix %s nexthop %s" % (pa_supernet, nexthop),
                      module_ignore_errors=True)

    # Verify the PA routes actually WIN over the DHCP midplane default. A /16 should
    # beat 0.0.0.0/0, but the midplane DHCP client has been seen to reclaim routing
    # (wrong VRF / route reordering), which silently black-holes the return. Probe
    # one IP per supernet with `ip route get` and confirm it egresses the dataplane
    # nexthop, NOT eth0-midplane.
    for probe in ("221.1.0.1", "221.2.0.1", "221.4.0.1"):
        rg = dpuhost.shell("ip route get %s" % probe, module_ignore_errors=True).get("stdout", "")
        rg1 = rg.splitlines()[0] if rg.strip() else "(no route)"
        if "midplane" in rg or nexthop not in rg:
            logger.warning("DPU: PA %s still NOT via dataplane nexthop %s -> RETURN WILL "
                           "BLACK-HOLE: %s", probe, nexthop, rg1)
        else:
            logger.info("DPU: PA %s routes via dataplane nexthop %s: %s", probe, nexthop, rg1)

    # Permanent static neighbor for the dataplane nexthop. NASA/DPDK builds the outer
    # Ethernet header of the re-encapped (NVGRE) return packet from this ARP entry;
    # without a resolved next-hop MAC the packet is dropped on egress (DPU0 Eth0 TX=0
    # even though RX is full). Resolve once via ping, then pin it PERMANENT so DPDK
    # always has the MAC and it survives ARP timeouts.
    egress_rg = dpuhost.shell("ip route get %s" % nexthop, module_ignore_errors=True).get("stdout", "")
    etoks = egress_rg.split()
    egress_dev = next((etoks[i + 1] for i, t in enumerate(etoks) if t == "dev"), "Ethernet0")
    dpuhost.shell("ping -c 3 -W 2 %s" % nexthop, module_ignore_errors=True)
    nbr = dpuhost.shell("ip neigh show %s" % nexthop, module_ignore_errors=True).get("stdout", "")
    ntoks = nbr.split()
    nh_mac = next((ntoks[i + 1] for i, t in enumerate(ntoks) if t == "lladdr"), None)
    if nh_mac:
        for attempt in range(3):
            dpuhost.shell("sudo ip neigh replace %s lladdr %s dev %s nud permanent"
                          % (nexthop, nh_mac, egress_dev), module_ignore_errors=True)
            chk = dpuhost.shell("ip neigh show %s" % nexthop,
                                module_ignore_errors=True).get("stdout", "")
            if "PERMANENT" in chk.upper():
                logger.info("DPU: permanent ARP %s lladdr %s dev %s (attempt %d)",
                            nexthop, nh_mac, egress_dev, attempt + 1)
                break
        else:
            logger.warning("DPU: failed to pin permanent ARP for %s (last: %s)", nexthop, chk.strip())
    else:
        logger.warning("DPU: could not resolve next-hop %s MAC (ping unanswered on dev %s) -> "
                       "NASA may drop the return on egress; check NPU Ethernet224 is up",
                       nexthop, egress_dev)

    logger.info("DPU: show ip route")
    dpu_route = dpuhost.shell("show ip route", module_ignore_errors=True)
    for line in dpu_route.get("stdout", "").splitlines():
        logger.info("  DPU route: %s", line)

    logger.info("DPU: show ip interfaces")
    dpu_ifaces = dpuhost.shell("show ip interfaces", module_ignore_errors=True)
    for line in dpu_ifaces.get("stdout", "").splitlines():
        logger.info("  DPU iface: %s", line)


def _count_json_operations(filepath):
    """Count SET/DEL operations and distinct table types in a config JSON file."""
    with open(filepath) as f:
        operations = json.load(f)
    tables = {}
    for op in operations:
        op_type = op.get("OP", "?")
        for k in op:
            if k == "OP":
                continue
            table = k.split(":")[0]
            tables.setdefault(table, {"SET": 0, "DEL": 0})
            tables[table][op_type] = tables[table].get(op_type, 0) + 1
    return len(operations), tables


def _verify_dpu_appl_db(dpuhost, table_pattern, label=""):
    """Query DPU_APPL_DB for keys matching table_pattern and return count + sample keys."""
    quiet = "DASH_VNET_MAPPING_TABLE" in table_pattern
    out = dpuhost.shell(f"sonic-db-cli DPU_APPL_DB KEYS '{table_pattern}' 2>/dev/null",
                        module_ignore_errors=True, verbose=not quiet)
    keys = [k.strip() for k in out.get("stdout", "").splitlines() if k.strip()]
    if label:
        logger.info("  DPU_APPL_DB %s: %d keys matching '%s'", label, len(keys), table_pattern)
        for k in keys[:5]:
            logger.info("    sample: %s", k)
        if len(keys) > 5:
            logger.info("    ... and %d more", len(keys) - 5)
    return keys


def _container_path_to_host(container_path):
    """Collapse a repeated adjacent dir (e.g. .../x/x/... → .../x/...) for Docker-in-Docker paths."""
    parts = container_path.split("/")
    for i in range(1, len(parts) - 1):
        if parts[i] and parts[i] == parts[i + 1]:
            candidate = "/".join(parts[:i] + parts[i + 1:])
            logger.info("Path translation: %s -> %s (collapsed '%s')", container_path, candidate, parts[i])
            return candidate
    return container_path


def _inspect_gnmi_server(config_facts):
    """Discover TLS mode and cert paths from the NPU's already-loaded CONFIG_DB.

    `client_auth` is the authoritative switch for mTLS: if it's true, the server
    will reject clients that don't present a cert, regardless of whether
    `GNMI.certs.ca_crt` is listed. Absence of server_crt/server_key means the
    server is running noTLS.

    The GNMI YANG model only accepts `server_crt`/`server_key`/`ca_crt` under
    `GNMI|certs` — `client_crt`/`client_key` break `config apply-patch`. So for
    mTLS we derive them by convention (`client.crt` / `client.key` alongside the
    CA or server cert) and let `_fetch_gnmi_certs_from_npu` skip any that are
    absent on disk.
    """
    gnmi_cfg = (config_facts or {}).get("GNMI", {}) or {}
    certs = gnmi_cfg.get("certs", {}) or {}
    gnmi = gnmi_cfg.get("gnmi", {}) or {}

    paths = {
        "server_crt": certs.get("server_crt"),
        "server_key": certs.get("server_key"),
        "ca_crt": certs.get("ca_crt"),
    }
    client_auth = str(gnmi.get("client_auth", "false")).lower() == "true"

    has_tls = bool(paths["server_crt"] and paths["server_key"])
    if not has_tls:
        mode = "insecure"
    elif client_auth:
        mode = "mtls"
    else:
        mode = "tls"

    if mode == "mtls":
        ref = paths["ca_crt"] or paths["server_crt"]
        if ref:
            cert_dir = os.path.dirname(ref)
            paths["client_crt"] = f"{cert_dir}/client.crt"
            paths["client_key"] = f"{cert_dir}/client.key"
            if not paths["ca_crt"]:
                paths["ca_crt"] = f"{cert_dir}/ca.crt"

    logger.info("CONFIG_DB GNMI.certs: %s", {k: v for k, v in paths.items() if v} or "(none)")
    logger.info("CONFIG_DB GNMI.gnmi.client_auth=%s → mode=%s", client_auth, mode)
    return {"mode": mode, "paths": paths, "client_auth": client_auth}


def _fetch_gnmi_certs_from_npu(duthost, env, paths, dest_dir):
    """Fetch only the cert files the server actually references (from CONFIG_DB).

    `paths` is the runtime mapping from `_inspect_gnmi_server()`. For each
    non-None entry we `docker cp` out of the gnmi container and `fetch` to
    dest_dir. Missing files are skipped with a warning — the server may not
    have all three (e.g. no ca_crt when client_auth is off). Returns
    {flag: basename} for every file successfully staged locally.
    """
    container = env.gnmi_container
    fetched = {}
    for flag, src in paths.items():
        if not src:
            continue
        name = os.path.basename(src)
        cp = duthost.shell(
            f"docker cp {container}:{src} /tmp/{name}",  # noqa: E231
            module_ignore_errors=True,
        )
        if cp.get("rc", 1) != 0:
            logger.warning("  docker cp %s failed, skipping: %s",
                           src, (cp.get("stderr", "") or "").strip()[:200])
            continue
        try:
            duthost.fetch(src=f"/tmp/{name}", dest=f"{dest_dir}/{name}", flat=True)
            fetched[flag] = name
            logger.info("  Fetched %s (%s) from %s", name, flag, src)
        except Exception as e:
            logger.warning("  fetch %s failed: %s", src, e)
    return fetched


_TRANSIENT_GNMI_MARKERS = ("unavailable", "socket closed", "failed to connect",
                           "error reading server preface", "connection reset")


def _detect_server_tls(duthost, env):
    """Inspect the *running* telemetry process on the NPU to decide client TLS mode.

    Returns 'notls' (plaintext), 'tls' (server cert only, no client cert) or
    'mtls' (server requires a client cert). The running process flags are
    authoritative: CONFIG_DB ``GNMI|certs`` is frequently empty even when the
    server is launched with ``--server_crt/--server_key/--ca_crt`` on the
    command line, so detection off CONFIG_DB alone misreads the mode."""
    out = duthost.shell(
        "docker exec %s bash -c \"ps -eo args | grep -- '--port %d' | grep -v grep\""  # noqa: E501
        % (env.gnmi_container, env.gnmi_port),
        module_ignore_errors=True,
    )
    line = (out.get("stdout", "") or "").strip()
    logger.info("NPU gNMI server cmdline: %s", line or "(not found)")
    low = line.lower()
    if not line or "--notls" in low or "-notls" in low:
        return "notls"
    if "allow_no_client_auth" in low:
        return "tls"
    if "ca_crt" in low:
        return "mtls"
    return "tls"


def _stage_npu_certs(duthost, env, dest_dir):
    """Copy CA + server cert/key off the NPU gnmi container to the controller.

    ``docker cp`` each file out of the ``gnmi`` container, then ``fetch`` it to
    ``dest_dir`` on the Ansible controller — the same host that runs the native
    gNMI client via ``localhost.shell``. Returns ``{'ca','cert','key'}`` of local
    paths, or ``None`` if any file can't be staged.

    The server cert is reused as the *client* cert: it is signed by the same CA
    and carries no EKU restriction, so it satisfies the server's ``client_auth``
    check. Its SAN includes the NPU mgmt IP, so connecting by IP needs no
    ``GNMI_TARGET_NAME`` override."""
    files = {"ca": env.gnmi_ca_cert, "cert": env.gnmi_server_cert, "key": env.gnmi_server_key}
    local = {}
    for tag, name in files.items():
        src = env.gnmi_cert_path + name
        cp = duthost.shell("docker cp %s:%s /tmp/%s" % (env.gnmi_container, src, name),  # noqa: E231
                           module_ignore_errors=True)
        if cp.get("rc", 1) != 0:
            logger.warning("  docker cp %s failed: %s", src, (cp.get("stderr", "") or "").strip()[:200])
            return None
        try:
            duthost.fetch(src="/tmp/%s" % name, dest="%s/%s" % (dest_dir, name), flat=True)
        except Exception as e:
            logger.warning("  fetch %s failed: %s", name, e)
            return None
        local[tag] = os.path.join(dest_dir, name)
        logger.info("  Staged NPU cert %s -> %s", name, local[tag])
    return local


def _gnmi_server_ready(localhost, ip, port, tls_paths=None):
    """True only if a gNMI Capabilities RPC succeeds.

    When ``tls_paths`` is given the probe negotiates TLS/mTLS with the NPU's
    own certs (reused as client certs); otherwise it uses a plaintext
    (``--noTLS``) insecure channel. We gate on a real RPC success (rc == 0)
    rather than mere TCP reachability, so a transport mismatch (plaintext probe
    vs. TLS server, or vice-versa) is correctly treated as not-ready."""
    if tls_paths:
        # Plain str + .format (not an f-string): keeps pycodestyle from
        # tokenizing the commas/semicolons of the embedded python under 3.12.
        probe = (
            "import grpc; from pygnmi.spec.v080 import gnmi_pb2, gnmi_pb2_grpc as g; "  # noqa: E702
            "creds=grpc.ssl_channel_credentials("
            "root_certificates=open({ca!r},'rb').read(),"
            "private_key=open({key!r},'rb').read(),"
            "certificate_chain=open({cert!r},'rb').read()); "
            "ch=grpc.secure_channel('{ip}:{port}',creds); "
            "g.gNMIStub(ch).Capabilities(gnmi_pb2.CapabilityRequest(), timeout=6)"
        ).format(ca=tls_paths["ca"], key=tls_paths["key"], cert=tls_paths["cert"], ip=ip, port=port)
    else:
        probe = (
            "import grpc; from pygnmi.spec.v080 import gnmi_pb2, gnmi_pb2_grpc as g; "  # noqa: E702
            f"g.gNMIStub(grpc.insecure_channel('{ip}:{port}'))."  # noqa: E231
            "Capabilities(gnmi_pb2.CapabilityRequest(), timeout=6)"
        )
    out = localhost.shell("python3 -c %s" % shlex.quote(probe), module_ignore_errors=True)
    return out.get("rc", 1) == 0


def _wait_gnmi_ready(localhost, ip, port, timeout=600, interval=5, tls_paths=None):
    """Block until the gNMI server answers a Capabilities RPC (or timeout).

    Probes over TLS when ``tls_paths`` is given, else plaintext. Returns True if
    ready. Timeout is generous to ride out a server restart at testbed setup."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _gnmi_server_ready(localhost, ip, port, tls_paths=tls_paths):
            logger.info("gNMI server %s:%s is ready", ip, port)
            return True
        logger.info("gNMI server %s:%s not ready (likely restarting) — waiting %ds ...",
                    ip, port, interval)
        time.sleep(interval)
    logger.warning("gNMI server %s:%s still not ready after %ds — proceeding anyway", ip, port, timeout)
    return False


def load_json_via_gnmi(localhost, duthost, dpuhost, config_facts, config_dir, files, timings,
                       creds, mem_timeline=None, push_events=None, on_file_done=None):
    """Push each JSON to the DPU via the native gNMI client.

    Uses ``tests/dash/gnmi_agent_extracted/gnmi_client.py``, which sends
    username/password as gRPC *metadata* over a channel matched to the NPU
    server's transport. The transport is auto-detected at runtime
    (:func:`_detect_server_tls`):
      * ``notls`` — plaintext ``grpc.insecure_channel`` (server in ``--noTLS``).
      * ``tls`` / ``mtls`` — the server's own CA + cert/key are copied off the
        ``gnmi`` container and reused as client certs (the server cert is
        CA-signed with no EKU restriction and its SAN covers the NPU IP), then
        passed to the client via the ``GNMI_CA`` / ``GNMI_CLIENT_CERT`` /
        ``GNMI_CLIENT_KEY`` env vars that ``go_gnmi_utils._open_channel`` reads.

    Optional instrumentation for the traffic test:
      * ``push_events`` — per-file ``{filename: {"idx", "kind", "start", "end"}}``
        with wall-clock timestamps.
      * ``on_file_done`` — ``on_file_done(filename, idx, kind, t_start, t_end)``
        called right after each file's push (lets the caller snapshot traffic).
    """
    if mem_timeline is None:
        mem_timeline = []
    env = GNMIEnvironment(duthost)
    dpu_index = dpuhost.dpu_index
    ip = duthost.mgmt_ip
    port = env.gnmi_port
    extracted_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), _GNMI_EXTRACTED_SUBDIR)
    gnmi_user = shlex.quote(creds["sonicadmin_user"])
    gnmi_pass = shlex.quote(creds["sonicadmin_password"])

    # Match the client transport to the NPU server's (it may run plaintext or
    # TLS/mTLS depending on testbed state). For TLS, reuse the NPU's own certs.
    mode = _detect_server_tls(duthost, env)
    tls_prefix = ""
    tls_paths = None
    if mode in ("tls", "mtls"):
        cert_dir = os.path.join(os.path.dirname(config_dir.rstrip("/\\")), "gnmi_certs")
        os.makedirs(cert_dir, exist_ok=True)
        tls_paths = _stage_npu_certs(duthost, env, cert_dir)
        if tls_paths:
            tls_prefix = "GNMI_CA=%s " % shlex.quote(tls_paths["ca"])
            if mode == "mtls":
                tls_prefix += "GNMI_CLIENT_CERT=%s GNMI_CLIENT_KEY=%s " % (
                    shlex.quote(tls_paths["cert"]), shlex.quote(tls_paths["key"]))
            logger.info("gNMI push via native client %s -> %s:%s (dpu %d, %s)",
                        extracted_dir, ip, port, dpu_index, mode.upper())
        else:
            logger.warning("Server is %s but staging NPU certs failed — "
                           "falling back to plaintext (push will likely fail)", mode.upper())
    else:
        logger.info("gNMI push via native client %s -> %s:%s (dpu %d, plaintext)",
                    extracted_dir, ip, port, dpu_index)

    # The server can bounce when its certs are regenerated at testbed setup —
    # wait for it to be accepting connections before we start timing.
    _wait_gnmi_ready(localhost, ip, port, tls_paths=tls_paths)

    db_before = dpuhost.shell("sonic-db-cli DPU_APPL_DB DBSIZE", module_ignore_errors=True)
    logger.info("DPU_APPL_DB DBSIZE before push: %s", db_before.get("stdout", "").strip())

    # Pre-count operations for all files (outside the timed loop).
    file_info = []
    all_tables = set()
    for filename in files:
        op_count, tables = _count_json_operations(os.path.join(config_dir, filename))
        file_info.append((filename, op_count, tables))
        all_tables.update(tables.keys())

    # TEMP phased-push: order apl/eni before map, barrier on ASIC_DB in between.
    if _PHASED_PUSH:
        file_info.sort(key=lambda fi: (
            {None: 0, "apl": 0, "eni": 1, "map": 2}.get(parse_file_index(fi[0])[1], 1),
            parse_file_index(fi[0])[0] if parse_file_index(fi[0])[0] is not None else -1))
        logger.info("PHASED push: %d files ordered apl/eni -> (ASIC barrier) -> map", len(file_info))
    _expected_vnets = sum(1 for fi in file_info if parse_file_index(fi[0])[1] == "eni")
    _barrier_done = not _PHASED_PUSH

    def _asic_count(obj_type):
        o = dpuhost.shell("sonic-db-cli ASIC_DB KEYS 'ASIC_STATE:SAI_OBJECT_TYPE_%s:*' | wc -l" % obj_type,
                          module_ignore_errors=True)
        try:
            return int((o.get("stdout", "") or "0").strip() or 0)
        except ValueError:
            return 0

    push_errors = []
    for idx, (filename, op_count, tables) in enumerate(file_info, start=1):
        if not _barrier_done and parse_file_index(filename)[1] == "map":
            logger.info("  PHASED BARRIER: all eni files pushed; polling ASIC_DB for %d VNETs ...", _expected_vnets)
            _bdl = time.time() + 600
            while time.time() < _bdl:
                vn, en = _asic_count("VNET"), _asic_count("ENI")
                logger.info("  PHASED BARRIER: ASIC_DB VNET=%d ENI=%d (expect %d)", vn, en, _expected_vnets)
                if vn >= _expected_vnets:
                    break
                time.sleep(5)
            logger.info("  PHASED BARRIER: done — pushing mappings now")
            _barrier_done = True
        table_summary = ", ".join(
            "{0}:{1}S/{2}D".format(t, tables[t]['SET'], tables[t]['DEL'])
            for t in sorted(tables)
        )
        logger.info("  [%d/%d] pushing %s (%d ops: %s) ...", idx, len(files), filename, op_count, table_summary)

        cfg_path = os.path.join(config_dir, filename)
        cmd = (
            f"cd {extracted_dir} && {tls_prefix}PYTHONPATH=. python3 gnmi_client.py"
            f" --batch_val {_BATCH_VAL} -l warning -t {ip}:{port} -i {dpu_index} -n 8"  # noqa: E231
            f" -u {gnmi_user} -p {gnmi_pass} update -f {cfg_path}"
        )

        # The server can bounce (cert regen / restart) and briefly drop the
        # connection. Gate each file on a ready server and retry transient
        # transport errors rather than aborting.
        eni_idx, kind = parse_file_index(filename)
        t_start = time.time()
        rc, stderr, stdout = -1, "", ""
        max_attempts = 8
        for attempt in range(1, max_attempts + 1):
            if not _gnmi_server_ready(localhost, ip, port, tls_paths=tls_paths):
                _wait_gnmi_ready(localhost, ip, port, tls_paths=tls_paths)
            out = localhost.shell(cmd, module_ignore_errors=True)
            rc = out.get("rc", -1)
            stderr = out.get("stderr", "") or ""
            stdout = out.get("stdout", "") or ""
            transient = rc != 0 and any(m in stderr.lower() for m in _TRANSIENT_GNMI_MARKERS)
            if rc == 0 or not transient:
                break
            logger.warning("  [%d/%d] %s: transient gNMI error (attempt %d/%d) — server bounced; "
                           "waiting for it to come back", idx, len(files), filename,
                           attempt, max_attempts)
            _wait_gnmi_ready(localhost, ip, port, tls_paths=tls_paths)
        t_end = time.time()
        elapsed = t_end - t_start
        timings[filename] = elapsed

        if push_events is not None:
            push_events[filename] = {"idx": eni_idx, "kind": kind, "start": t_start, "end": t_end}
        if on_file_done is not None:
            try:
                on_file_done(filename, eni_idx, kind, t_start, t_end)
            except Exception:
                logger.exception("  on_file_done callback failed (non-fatal)")

        failed = False
        reason = ""
        if rc != 0:
            failed = True
            reason = f"exit code {rc}"
        elif "Traceback" in stderr or "RpcError" in stderr or "Set failed" in stderr:
            failed = True
            reason = "error string in output"

        if failed:
            logger.error("  [%d/%d] FAILED %s after %.2fs — %s\n  output (tail): %s",
                         idx, len(files), filename, elapsed, reason, (stderr or stdout)[-3000:])
            push_errors.append(f"{filename}: {reason}")
        else:
            logger.info("  [%d/%d] done    %-40s  %.2fs  rc=%d", idx, len(files), filename, elapsed, rc)

        # ── Per-file memory snapshot (lightweight — free -m only) ──
        try:
            npu_mem = _collect_free_memory(duthost)
            dpu_mem = _collect_free_memory(dpuhost)
            mem_timeline.append({
                "idx": idx,
                "file": filename,
                "ops": op_count,
                "npu_free": npu_mem.get("_system_free", 0),
                "npu_available": npu_mem.get("_system_available", 0),
                "dpu_free": dpu_mem.get("_system_free", 0),
                "dpu_available": dpu_mem.get("_system_available", 0),
            })
        except Exception:
            logger.debug("  [%d/%d] mem snapshot failed (non-fatal)", idx, len(files))

    # Batch verification: check DPU_APPL_DB once for all tables.
    for table in sorted(all_tables):
        _verify_dpu_appl_db(dpuhost, "%s:*" % table, label="after all files")

    db_after = dpuhost.shell("sonic-db-cli DPU_APPL_DB DBSIZE", module_ignore_errors=True)
    logger.info("DPU_APPL_DB DBSIZE after push: %s (was: %s)",
                db_after.get("stdout", "").strip(), db_before.get("stdout", "").strip())

    if push_errors:
        pytest.fail("gNMI push had %d error(s):\n%s" % (
            len(push_errors), "\n".join("  - %s" % e for e in push_errors)))
