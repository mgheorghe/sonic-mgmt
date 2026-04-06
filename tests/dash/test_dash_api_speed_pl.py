import fnmatch
import logging
import os
import re
import time

import pytest

logger = logging.getLogger(__name__)

pytestmark = [
    pytest.mark.topology("smartswitch"),
    pytest.mark.skip_check_dut_health,
    pytest.mark.disable_loganalyzer,
    pytest.mark.sanity_check(skip_sanity=True),
]

CONFIG_DIR = os.path.join(os.path.dirname(__file__), "configs", "private-link-50")

# Path on NPU where JSON files are staged for the docker mount
_NPU_STAGE_DIR = "/tmp/dash_load"

# gnmi-agent container image and fixed paths expected on the NPU
_GNMI_AGENT_IMAGE = "sonic-gnmi-agent:2026march13"
_GO_GNMI_UTILS_NPU = "/root/pl_1/go_gnmi_utils.py"
_GO_GNMI_UTILS_CTR = "/usr/lib/python3/dist-packages/gnmi_agent/go_gnmi_utils.py"


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


def _print_results(timings, total_elapsed, mem_before, mem_after, redis_before, redis_after):
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


def test_dash_api_load_speed_pl(duthost, dpuhosts, dpu_index):
    """
    Measure the time to load private-link-50 DASH configs onto a DPU via gNMI.

    For each JSON config file:
      1. Copy the file to _NPU_STAGE_DIR on the NPU.
      2. Run sonic-gnmi-agent docker container on the NPU with the stage dir
         mounted, invoking gnmi_client.py to push the file to the DPU.
      3. Record and log the time taken per file.
    """
    dpuhost = dpuhosts[dpu_index]

    # ── Pre-flight: verify DPU is alive via SSH port check ───────────────────
    # ping and 'show chassis module midplane-status' are both unreliable after
    # a previous run removed the midplane default route. Check TCP port 22
    # instead — if SSH is listening the DPU is up.
    dpu_name = f"DPU{dpuhost.dpu_index}"
    dpu_midplane_ip = "169.254.200.%d" % (dpuhost.dpu_index + 1)
    logger.info("Pre-flight: checking SSH port on %s (%s) ...", dpu_name, dpu_midplane_ip)
    ssh_check = duthost.shell(
        f"nc -zw 5 {dpu_midplane_ip} 22", module_ignore_errors=True
    )
    assert ssh_check.get("rc", 1) == 0, (
        f"{dpu_name} SSH port 22 is not reachable at {dpu_midplane_ip}. "
        "DPU is not up — aborting test."
    )
    logger.info("%s is up — SSH port 22 reachable at %s", dpu_name, dpu_midplane_ip)

    config_dir = os.path.join(CONFIG_DIR, f"dpu{dpuhost.dpu_index}")

    assert os.path.isdir(config_dir), \
        f"Config directory not found: {config_dir}"

    pattern = f"*dpu{dpuhost.dpu_index}*.json"
    files = sorted(
        f for f in os.listdir(config_dir)
        if fnmatch.fnmatch(f, pattern) and f.endswith(".json")
    )
    assert files, f"No JSON config files found matching '{pattern}' in {config_dir}"
    files = files[:3]
    logger.info(
        "Found config files to load for dpu%d (limited to %d for this run)",
        dpuhost.dpu_index, len(files),
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

    dataplane_defaults = [
        route for route in routes_out.get("stdout", "").splitlines()
        if route.startswith("default") and "169.254.200.254" not in route
    ]
    assert dataplane_defaults, \
        "No dataplane default route found after removing midplane routes. " \
        "'ip route show default': %s" % routes_out.get("stdout", "")
    logger.info("DPU: active default route(s): %s", "; ".join(dataplane_defaults))

    # Add permanent static neighbor (ARP) entries on NPU for dataplane next-hops.
    logger.info("NPU: adding permanent static ARP entries for dataplane next-hops")
    _NPU_STATIC_ARP = [
        ("220.0.1.2", "80:09:02:02:00:01"),
        ("220.0.2.2", "80:09:02:02:00:02"),
        ("220.0.3.2", "80:09:02:02:00:03"),
        ("220.0.4.2", "80:09:02:02:00:04"),
    ]
    for ip, mac in _NPU_STATIC_ARP:
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

    dpu_dataplane_ip = "10.0.0.%d" % (57 + dpuhost.dpu_index * 2)

    logger.info("NPU: pinging DPU midplane IP %s to populate ARP", dpu_midplane_ip)
    duthost.shell(f"ping -c 3 -W 2 {dpu_midplane_ip}", module_ignore_errors=True)

    arp_out = duthost.shell(f"ip n show {dpu_midplane_ip}", module_ignore_errors=True)
    logger.info("NPU ARP entry for %s: %s", dpu_midplane_ip,
                arp_out.get("stdout", "").strip() or "(none)")

    logger.info("NPU: pinging DPU dataplane IP %s", dpu_dataplane_ip)
    ping_out = duthost.shell(f"ping -c 5 -W 2 {dpu_dataplane_ip}", module_ignore_errors=True)
    for line in ping_out.get("stdout", "").splitlines():
        logger.info("  %s", line)

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

    # ── Prepare stage directory on NPU ────────────────────────────────────────
    duthost.shell(f"mkdir -p {_NPU_STAGE_DIR}", module_ignore_errors=True)

    timings = {}
    total_start = time.time()

    # ── Load each JSON file via docker run on NPU ─────────────────────────────
    for idx, filename in enumerate(files, start=1):
        local_path = os.path.join(config_dir, filename)
        npu_path = f"{_NPU_STAGE_DIR}/{filename}"

        duthost.copy(src=local_path, dest=npu_path, verbose=False)
        logger.info("  [%d/%d] pushing %s ...", idx, len(files), filename)

        cmd = (
            "docker run --network host"
            f" --mount src={_NPU_STAGE_DIR},target=/dpu,type=bind,readonly"  # noqa: E231
            f" --mount src={_GO_GNMI_UTILS_NPU},target={_GO_GNMI_UTILS_CTR},type=bind,readonly"  # noqa: E231
            f" -t {_GNMI_AGENT_IMAGE}"
            f" -c 'gnmi_client.py --batch_val 500 -i {dpuhost.dpu_index}"
            f" -n 8 -t 127.0.0.1:50052 update -f /dpu/{filename}'"  # noqa: E231
        )

        t_start = time.time()
        out = duthost.shell(cmd, module_ignore_errors=True)
        elapsed = time.time() - t_start
        timings[filename] = elapsed

        stdout = out.get("stdout", "")
        for line in stdout.splitlines():
            logger.info("    %s", line)

        if "Set failed" in stdout or out.get("rc", 0) != 0:
            logger.error("  [%d/%d] FAILED %s after %.2fs", idx, len(files), filename, elapsed)
            logger.error("  stderr: %s", out.get("stderr", ""))
            pytest.fail(f"gnmi_client.py failed for {filename}: {stdout}")

        logger.info("  [%d/%d] done    %-40s  %.2fs", idx, len(files), filename, elapsed)

        duthost.shell(f"rm -f {npu_path}", module_ignore_errors=True)

    total_elapsed = time.time() - total_start

    # ── Check DPU is still up after the push ──────────────────────────────────
    # Midplane reachability (169.254.200.x) is expected to be False after we
    # removed the midplane default route, so it is not a reliable crash indicator.
    # Instead, ping the dataplane IP — if that also fails the DPU is truly down.
    midplane_out = duthost.show_and_parse("show chassis module midplane-status")
    dpu_row = next((r for r in midplane_out if r.get("name", "").strip().upper() == dpu_name), None)
    midplane_reachability = dpu_row.get("reachability", "").strip() if dpu_row else "unknown"
    logger.info("%s midplane reachability after push: %s (expected False — midplane route removed)",
                dpu_name, midplane_reachability)

    logger.info("Verifying %s is alive via dataplane ping to %s ...", dpu_name, dpu_dataplane_ip)
    ping_out = duthost.shell(f"ping -c 3 -W 2 {dpu_dataplane_ip}", module_ignore_errors=True)
    ping_ok = ping_out.get("rc", 1) == 0
    for line in ping_out.get("stdout", "").splitlines():
        logger.info("  %s", line)
    assert ping_ok, (
        f"{dpu_name} is unreachable via dataplane IP {dpu_dataplane_ip} after push. "
        "DPU may have crashed — check DPU logs."
    )
    logger.info("%s dataplane reachability after push: OK", dpu_name)

    # ── Verify all 64 ENIs are programmed on DPU ──────────────────────────────
    logger.info("DPU: checking ENI count in COUNTERS_DB...")
    eni_out = dpuhost.shell(
        'sonic-db-cli COUNTERS_DB HGETALL "COUNTERS_ENI_NAME_MAP"',
        module_ignore_errors=True,
    )
    eni_lines = [line.strip() for line in eni_out.get("stdout", "").splitlines() if line.strip()]
    eni_count = len(eni_lines) // 2
    logger.info("DPU: ENIs found in COUNTERS_ENI_NAME_MAP: %d", eni_count)
    for line in eni_lines:
        logger.info("  %s", line)
    assert eni_count == 64, \
        "Expected 64 ENIs in COUNTERS_ENI_NAME_MAP but found %d" % eni_count

    mem_after = {
        "NPU": _collect_memory(duthost),
        "DPU": _collect_memory(dpuhost),
    }
    redis_after = _collect_redis_memory(dpuhost)

    _print_results(timings, total_elapsed, mem_before, mem_after, redis_before, redis_after)
