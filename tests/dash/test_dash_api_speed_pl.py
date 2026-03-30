import fnmatch
import logging
import os
import re
import time

import pytest

from gnmi_utils import apply_gnmi_file

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

    out = host.shell(
        'docker stats --no-stream --format "{{.Name}}\t{{.MemUsage}}"',
        module_ignore_errors=True,
    )
    for line in out["stdout"].splitlines():
        line = line.strip()
        if not line or "\t" not in line:
            continue
        name, mem_usage = line.split("\t", 1)
        used_str = mem_usage.split("/")[0].strip()
        result[name.strip()] = _parse_mem_str(used_str)

    free_out = host.shell("free -m", module_ignore_errors=True)
    for line in free_out["stdout"].splitlines():
        if line.startswith("Mem:"):
            parts = line.split()
            result["_system_used"] = float(parts[2])

    return result


def _print_results(timings, total_elapsed, mem_before, mem_after):
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

    logger.info(sep)


def test_dash_api_load_speed_pl(localhost, duthost, ptfhost, dpuhosts, dpu_index):
    """
    Measure the time to load private-link-50 DASH configs onto a DPU via gNMI.

    Loads all JSON files from configs/private-link-50/dpu{dpu_index}/ in sorted
    order, recording per-file and total load times. Prints a summary table with
    timing and NPU/DPU per-container memory before/after deltas.
    """
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
    logger.info("Found %d config files to load for dpu%d", len(files), dpuhost.dpu_index)

    mem_before = {
        "NPU": _collect_memory(duthost),
        "DPU": _collect_memory(dpuhost),
    }

    timings = {}
    total_start = time.time()

    for filename in files:
        local_path = os.path.join(config_dir, filename)
        dut_path = f"/tmp/{filename}"

        duthost.copy(src=local_path, dest=dut_path, verbose=False)

        t_start = time.time()
        apply_gnmi_file(
            localhost,
            duthost,
            ptfhost,
            dest_path=dut_path,
            wait_after_apply=0,
            host=f"dpu{dpuhost.dpu_index}",
        )
        elapsed = time.time() - t_start
        timings[filename] = elapsed
        logger.info("  loaded %-40s  %.2fs", filename, elapsed)

        duthost.shell(f"rm -f {dut_path}", module_ignore_errors=True)

    total_elapsed = time.time() - total_start

    mem_after = {
        "NPU": _collect_memory(duthost),
        "DPU": _collect_memory(dpuhost),
    }

    _print_results(timings, total_elapsed, mem_before, mem_after)
