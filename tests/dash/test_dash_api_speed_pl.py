import fnmatch
import json
import logging
import os
import re
import time

import pytest

import proto_utils
from gnmi_utils import GNMIEnvironment, write_gnmi_files

logger = logging.getLogger(__name__)

pytestmark = [
    pytest.mark.topology("smartswitch"),
    pytest.mark.skip_check_dut_health,
    pytest.mark.disable_loganalyzer,
    pytest.mark.sanity_check(skip_sanity=True),
]

CONFIG_DIR = os.path.join(os.path.dirname(__file__), "configs", "private-link-50")


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
    """
    Return a dict with per-container memory (MiB) keyed by container name,
    plus '_system_used' for total system used MiB from `free -m`.
    """
    result = {}

    # Use awk to avoid Jinja2 templating issues with Go's {{.Name}} format syntax.
    # docker stats columns: ID NAME CPU% MEM_USED / MEM_LIMIT MEM% ...
    out = host.shell(
        "docker stats --no-stream | awk 'NR>1 {print $2\"\\t\"$4}'",
        module_ignore_errors=True,
    )
    for line in out.get("stdout", "").splitlines():
        line = line.strip()
        if not line or "\t" not in line:
            continue
        name, used_str = line.split("\t", 1)
        result[name.strip()] = _parse_mem_str(used_str.strip())

    free_out = host.shell("free -m", module_ignore_errors=True)
    for line in free_out.get("stdout", "").splitlines():
        if line.startswith("Mem:"):
            parts = line.split()
            result["_system_used"] = float(parts[2])

    return result


def _collect_redis_memory(dpuhost):
    """
    Return a dict with Redis memory info from DPU_APPL_DB:
      '_used_memory_human' — overall used memory (human string)
      '_used_memory'       — overall used memory (bytes, int)
      per VNET_MAPPING key (2 samples) — per-key MEMORY USAGE in bytes
    """
    result = {}

    info_out = dpuhost.shell("sonic-db-cli DPU_APPL_DB INFO MEMORY",
                             module_ignore_errors=True)
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
        module_ignore_errors=True,
    )
    for key in keys_out.get("stdout", "").splitlines():
        key = key.strip()
        if not key:
            continue
        usage_out = dpuhost.shell(
            f"sonic-db-cli DPU_APPL_DB MEMORY USAGE '{key}'",
            module_ignore_errors=True,
        )
        try:
            result[key] = int(usage_out.get("stdout", "0").strip())
        except ValueError:
            result[key] = 0

    return result


def _print_results(timings, prep_elapsed, gnmi_elapsed, total_elapsed,
                   mem_before, mem_after, redis_before, redis_after):
    sep = "=" * 72
    logger.info(sep)
    logger.info("  DASH API LOAD SPEED TEST — RESULTS")
    logger.info(sep)

    logger.info("\n  Per-file proto serialization times:")
    logger.info("  %-44s  %8s", "File", "Time (s)")
    logger.info("  " + "-" * 56)
    for filename, elapsed in timings.items():
        logger.info("  %-44s  %8.2f", filename, elapsed)
    logger.info("  " + "-" * 56)
    logger.info("  %-44s  %8.2f", "Serialization total", prep_elapsed)
    logger.info("  %-44s  %8.2f", "gNMI push (tar+scp+set)", gnmi_elapsed)
    logger.info("  " + "-" * 56)
    logger.info("  %-44s  %8.2f", "TOTAL", total_elapsed)
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
        logger.info(
            "  %-30s  %8.1f  %8.1f  %+8.1f",
            "Containers total",
            total_before,
            total_after,
            total_after - total_before,
        )

        sys_b = before.get("_system_used", 0.0)
        sys_a = after.get("_system_used", 0.0)
        logger.info(
            "  %-30s  %8.1f  %8.1f  %+8.1f",
            "System used (free -m)",
            sys_b,
            sys_a,
            sys_a - sys_b,
        )

    # Redis (DPU_APPL_DB) memory
    logger.info("\n  DPU Redis memory — DPU_APPL_DB (bytes):")
    logger.info("  %-52s  %10s  %10s  %10s", "Key", "Before", "After", "Delta")
    logger.info("  " + "-" * 86)

    redis_b_total = redis_before.get("_used_memory", 0)
    redis_a_total = redis_after.get("_used_memory", 0)
    logger.info(
        "  %-52s  %10d  %10d  %+10d",
        "used_memory (total)",
        redis_b_total,
        redis_a_total,
        redis_a_total - redis_b_total,
    )
    logger.info(
        "  %-52s  %10s  %10s",
        "used_memory_human",
        redis_before.get("_used_memory_human", "n/a"),
        redis_after.get("_used_memory_human", "n/a"),
    )

    sample_keys = sorted(k for k in set(redis_before) | set(redis_after) if not k.startswith("_"))
    for key in sample_keys:
        b = redis_before.get(key, 0)
        a = redis_after.get(key, 0)
        logger.info("  %-52s  %10d  %10d  %+10d", key, b, a, a - b)

    logger.info(sep)


def test_dash_api_load_speed_pl(localhost, duthost, ptfhost, dpuhosts, dpu_index):
    """
    Measure the time to load private-link-50 DASH configs onto a DPU via gNMI.

    Phase 1 (per-file): Read each JSON file locally and serialize all entries to
    protobuf files in a single shared work directory — no SSH calls.

    Phase 2 (once): tar + SCP to PTF + gnmi_set all updates in one shot.

    This eliminates N×(tar+SCP+extract+gnmi_set+cleanup) and replaces it with
    a single pass, reducing SSH overhead from O(N) to O(1).
    """
    batch_size = 1024
    dpuhost = dpuhosts[dpu_index]
    config_dir = os.path.join(CONFIG_DIR, f"dpu{dpuhost.dpu_index}")

    assert os.path.isdir(config_dir), \
        f"Config directory not found: {config_dir}"

    pattern = f"*dpu{dpuhost.dpu_index}*.json"
    files = sorted(
        f for f in os.listdir(config_dir)
        if fnmatch.fnmatch(f, pattern) and f.endswith(".json")
    )
    assert files, f"No JSON config files found matching '{pattern}' in {config_dir}"
    logger.info(
        "Found %d config files to load for dpu%d (batch_size=%d)",
        len(files), dpuhost.dpu_index, batch_size,
    )

    # ── Pre-flight: DPU network setup ────────────────────────────────────────
    dpu_midplane_ip = "169.254.200.%d" % (dpuhost.dpu_index + 1)

    # Add Loopback0 IP on DPU and verify it was applied.
    loopback_ip = "221.0.0.%d/32" % (dpuhost.dpu_index + 1)
    logger.info("DPU: adding Loopback0 IP %s", loopback_ip)
    dpuhost.shell("sudo config interface ip add Loopback0 %s" % loopback_ip)
    iface_out = dpuhost.shell("show ip interfaces")
    assert "221.0.0.%d" % (dpuhost.dpu_index + 1) in iface_out.get("stdout", ""), \
        "Loopback0 IP %s was not found in 'show ip interfaces' after config" % loopback_ip

    # Remove ALL default routes via the midplane gateway (static + DHCP-installed).
    # Loop until none remain — DHCP may have installed multiple entries.
    logger.info("DPU: removing all default routes via 169.254.200.254")
    for _ in range(10):
        routes_out = dpuhost.shell("ip route show default", module_ignore_errors=True)
        midplane_defaults = [
            route for route in routes_out.get("stdout", "").splitlines()
            if "169.254.200.254" in route
        ]
        if not midplane_defaults:
            break
        dpuhost.shell("sudo ip route del default via 169.254.200.254",
                      module_ignore_errors=True)
    routes_out = dpuhost.shell("ip route show default", module_ignore_errors=True)
    remaining = [
        route for route in routes_out.get("stdout", "").splitlines()
        if "169.254.200.254" in route
    ]
    assert not remaining, \
        "Midplane default route(s) still present after removal: %s" % remaining

    # Verify a dataplane default route is now active.
    dataplane_defaults = [
        route for route in routes_out.get("stdout", "").splitlines()
        if route.startswith("default") and "169.254.200.254" not in route
    ]
    assert dataplane_defaults, \
        "No dataplane default route found after removing midplane routes. 'ip route show default': %s" \
        % routes_out.get("stdout", "")
    logger.info("DPU: active default route(s): %s", "; ".join(dataplane_defaults))

    # Add permanent static neighbor (ARP) entries on NPU for dataplane next-hops.
    # Use 'ip neigh replace ... nud permanent' so entries are not subject to ARP resolution.
    # The outgoing interface is resolved dynamically via 'ip route get'.
    logger.info("NPU: adding permanent static ARP entries for dataplane next-hops")
    _NPU_STATIC_ARP = [
        ("220.0.1.2", "80:09:02:02:00:01"),
        ("220.0.2.2", "80:09:02:02:00:02"),
        ("220.0.3.2", "80:09:02:02:00:03"),
        ("220.0.4.2", "80:09:02:02:00:04"),
    ]
    for ip, mac in _NPU_STATIC_ARP:
        # Find the egress interface for this IP.
        route_out = duthost.shell(f"ip route get {ip}", module_ignore_errors=True)
        dev = None
        for token in route_out.get("stdout", "").split():
            if token == "dev":
                idx = route_out.get("stdout", "").split().index("dev")
                dev = route_out.get("stdout", "").split()[idx + 1]
                break
        assert dev, f"Could not determine egress interface for {ip} on NPU"

        for attempt in range(3):
            duthost.shell(
                f"sudo ip neigh replace {ip} lladdr {mac} dev {dev} nud permanent",
                module_ignore_errors=True,
            )
            verify = duthost.shell(f"ip neigh show {ip}", module_ignore_errors=True)
            if "PERMANENT" in verify.get("stdout", "").upper():
                logger.info("  NPU: permanent ARP %s lladdr %s dev %s (attempt %d)",
                            ip, mac, dev, attempt + 1)
                break
        else:
            raise AssertionError(
                f"Failed to add permanent ARP entry for {ip} after 3 attempts. "
                f"'ip neigh show {ip}': {verify.get('stdout', '')}"
            )

    # Dataplane IP: 10.0.0.(56 + dpu_index*2 + 1), e.g. dpu0 → 10.0.0.57
    dpu_dataplane_ip = "10.0.0.%d" % (57 + dpuhost.dpu_index * 2)

    # Populate NPU ARP table for the DPU midplane IP.
    logger.info("NPU: pinging DPU midplane IP %s to populate ARP", dpu_midplane_ip)
    duthost.shell(f"ping -c 3 -W 2 {dpu_midplane_ip}", module_ignore_errors=True)

    # Verify ARP entry on NPU.
    arp_out = duthost.shell(f"ip n show {dpu_midplane_ip}", module_ignore_errors=True)
    logger.info("NPU ARP entry for %s: %s", dpu_midplane_ip,
                arp_out.get("stdout", "").strip() or "(none)")

    # Ping DPU dataplane IP from NPU to verify dataplane reachability.
    logger.info("NPU: pinging DPU dataplane IP %s", dpu_dataplane_ip)
    ping_out = duthost.shell(f"ping -c 5 -W 2 {dpu_dataplane_ip}", module_ignore_errors=True)
    for line in ping_out.get("stdout", "").splitlines():
        logger.info("  %s", line)

    # Diagnostic: routing and interfaces on both NPU and DPU.
    logger.info("NPU: show ip route")
    npu_route = duthost.shell("show ip route", module_ignore_errors=True)
    for line in npu_route.get("stdout", "").splitlines():
        logger.info("  NPU route: %s", line)

    logger.info("NPU: show ip interfaces")
    npu_ifaces = duthost.shell("show ip interfaces", module_ignore_errors=True)
    for line in npu_ifaces.get("stdout", "").splitlines():
        logger.info("  NPU iface: %s", line)

    logger.info("DPU: show ip route")
    dpu_route = dpuhost.shell("show ip route", module_ignore_errors=True)
    for line in dpu_route.get("stdout", "").splitlines():
        logger.info("  DPU route: %s", line)

    logger.info("DPU: show ip interfaces")
    dpu_ifaces = dpuhost.shell("show ip interfaces", module_ignore_errors=True)
    for line in dpu_ifaces.get("stdout", "").splitlines():
        logger.info("  DPU iface: %s", line)

    mem_before = {
        "NPU": _collect_memory(duthost),
        "DPU": _collect_memory(dpuhost),
    }
    redis_before = _collect_redis_memory(dpuhost)

    # One shared environment / work directory for all proto files.
    env = GNMIEnvironment(duthost)
    os.makedirs(env.work_dir, exist_ok=True)

    delete_list = []
    update_list = []
    update_cnt = 0
    timings = {}
    total_start = time.time()

    # ── Phase 1: serialize all files locally, no SSH ──────────────────────────
    for filename in files:
        local_path = os.path.join(config_dir, filename)
        t_start = time.time()

        with open(local_path) as f:
            operations = json.load(f)

        for operation in operations:
            if operation["OP"] == "SET":
                for k, v in operation.items():
                    if k == "OP":
                        continue
                    update_cnt += 1
                    proto_filename = "update%u" % update_cnt
                    message = proto_utils.parse_dash_proto(k, v)
                    with open(env.work_dir + proto_filename, "wb") as pf:
                        pf.write(message.SerializeToString())
                    keys = k.split(":", 1)
                    gnmi_key = keys[0] + "[key=" + keys[1] + "]"
                    path = "/DPU_APPL_DB/dpu%d/%s:$/root/%s" % (  # noqa: E228
                        dpuhost.dpu_index, gnmi_key, proto_filename,
                    )
                    update_list.append(path)
            elif operation["OP"] == "DEL":
                for k, v in operation.items():
                    if k == "OP":
                        continue
                    keys = k.split(":", 1)
                    gnmi_key = keys[0] + "[key=" + keys[1] + "]"
                    path = "/DPU_APPL_DB/dpu%d/%s" % (dpuhost.dpu_index, gnmi_key)  # noqa: E228
                    delete_list.append(path)

        elapsed = time.time() - t_start
        timings[filename] = elapsed
        logger.info("  prepared %-40s  %.2fs  (%d updates so far)", filename, elapsed, update_cnt)

    prep_elapsed = time.time() - total_start
    logger.info("Serialization done: %d updates, %d deletes in %.2fs",
                len(update_list), len(delete_list), prep_elapsed)

    # ── Phase 2: single tar + SCP + gnmi_set ──────────────────────────────────
    logger.info("Starting gNMI push (single tar+scp+set)...")
    t_gnmi = time.time()
    write_gnmi_files(localhost, duthost, ptfhost, env, delete_list, update_list, batch_size)
    gnmi_elapsed = time.time() - t_gnmi

    total_elapsed = time.time() - total_start

    # ── Verify all 64 ENIs are programmed on DPU ──────────────────────────────
    # Poll up to 5 minutes — DPU may need time to process 41k entries.
    expected_enis = 64
    eni_poll_timeout = 300
    eni_poll_interval = 10
    logger.info("DPU: waiting up to %ds for %d ENIs in COUNTERS_ENI_NAME_MAP...",
                eni_poll_timeout, expected_enis)

    eni_lines = []
    deadline = time.time() + eni_poll_timeout
    while time.time() < deadline:
        eni_out = dpuhost.shell(
            'sonic-db-cli COUNTERS_DB HGETALL "COUNTERS_ENI_NAME_MAP"',
            module_ignore_errors=True,
        )
        eni_lines = [
            line.strip()
            for line in eni_out.get("stdout", "").splitlines()
            if line.strip()
        ]
        eni_count = len(eni_lines) // 2
        logger.info("DPU: ENIs found so far: %d / %d", eni_count, expected_enis)
        if eni_count >= expected_enis:
            break
        time.sleep(eni_poll_interval)

    eni_count = len(eni_lines) // 2
    for line in eni_lines:
        logger.info("  %s", line)

    # Diagnostics: check DPU_APPL_DB to distinguish push failure vs processing failure.
    appl_eni_out = dpuhost.shell(
        "sonic-db-cli DPU_APPL_DB KEYS 'DASH_ENI_TABLE:*' 2>/dev/null | wc -l",
        module_ignore_errors=True,
    )
    logger.info("DPU: DASH_ENI_TABLE entries in DPU_APPL_DB: %s",
                appl_eni_out.get("stdout", "").strip())
    appl_vnet_out = dpuhost.shell(
        "sonic-db-cli DPU_APPL_DB KEYS 'DASH_VNET_MAPPING_TABLE:*' 2>/dev/null | wc -l",
        module_ignore_errors=True,
    )
    logger.info("DPU: DASH_VNET_MAPPING_TABLE entries in DPU_APPL_DB: %s",
                appl_vnet_out.get("stdout", "").strip())

    assert eni_count == expected_enis, \
        "Expected %d ENIs in COUNTERS_ENI_NAME_MAP but found %d after %ds" % (
            expected_enis, eni_count, eni_poll_timeout)

    mem_after = {
        "NPU": _collect_memory(duthost),
        "DPU": _collect_memory(dpuhost),
    }
    redis_after = _collect_redis_memory(dpuhost)

    _print_results(timings, prep_elapsed, gnmi_elapsed, total_elapsed,
                   mem_before, mem_after, redis_before, redis_after)
