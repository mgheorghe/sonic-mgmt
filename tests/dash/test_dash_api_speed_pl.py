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


def _print_results(timings, prep_elapsed, gnmi_elapsed, total_elapsed, mem_before, mem_after):
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

    # Configure Loopback0 IP on DPU (needed for dataplane routing).
    logger.info("DPU: adding Loopback0 IP 221.0.0.%d/32", dpuhost.dpu_index + 1)
    dpuhost.shell("sudo config interface ip add Loopback0 221.0.0.%d/32" % (dpuhost.dpu_index + 1),
                  module_ignore_errors=True)

    # Remove DPU midplane default route so the dataplane default route takes effect.
    logger.info("DPU: removing midplane default route via 169.254.200.254")
    dpuhost.shell("sudo ip route del 0.0.0.0/0 via 169.254.200.254",
                  module_ignore_errors=True)

    # Add static ARP entries on NPU for dataplane next-hops.
    logger.info("NPU: adding static ARP entries for dataplane next-hops")
    _NPU_STATIC_ARP = [
        ("220.0.1.2", "80:09:02:02:00:01"),
        ("220.0.2.2", "80:09:02:02:00:02"),
        ("220.0.3.2", "80:09:02:02:00:03"),
        ("220.0.4.2", "80:09:02:02:00:04"),
    ]
    for ip, mac in _NPU_STATIC_ARP:
        duthost.shell(f"arp -s {ip} {mac}", module_ignore_errors=True)
        logger.info("  NPU: arp -s %s %s", ip, mac)

    # Populate NPU ARP table for the DPU midplane IP.
    logger.info("NPU: pinging DPU midplane IP %s to populate ARP", dpu_midplane_ip)
    duthost.shell(f"ping -c 3 -W 2 {dpu_midplane_ip}", module_ignore_errors=True)

    # Verify ARP entry on NPU.
    arp_out = duthost.shell(f"ip n show {dpu_midplane_ip}", module_ignore_errors=True)
    logger.info("NPU ARP entry for %s: %s", dpu_midplane_ip,
                arp_out.get("stdout", "").strip() or "(none)")

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

    mem_after = {
        "NPU": _collect_memory(duthost),
        "DPU": _collect_memory(dpuhost),
    }

    _print_results(timings, prep_elapsed, gnmi_elapsed, total_elapsed, mem_before, mem_after)
