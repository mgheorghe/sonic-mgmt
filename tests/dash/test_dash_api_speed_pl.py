import fnmatch
import importlib.util
import logging
import os
import re
import shutil
import sys
import tempfile
import time

import pytest
from dash_api_speed_common import (
    _collect_memory,
    _collect_redis_memory,
    _print_results,
    dpu_pre_config,
    load_json_via_gnmi,
    npu_pre_config,
)

_RENDER_PATH = os.path.join(os.path.dirname(__file__), "configs", "dash_api_speed_pl", "render.py")
_render_spec = importlib.util.spec_from_file_location("dash_render", _RENDER_PATH)
render = importlib.util.module_from_spec(_render_spec)
# Register before exec_module so multiprocessing workers can pickle/unpickle
# top-level functions (_render_apl etc.) by their __module__ name.
sys.modules["dash_render"] = render
_render_spec.loader.exec_module(render)

logger = logging.getLogger(__name__)

pytestmark = [
    pytest.mark.topology("smartswitch"),
    pytest.mark.skip_check_dut_health,
    pytest.mark.disable_loganalyzer,
    pytest.mark.sanity_check(skip_sanity=True),
]


# How many ENIs to push per DPU. "ALL" = every rendered file.
# Int N = apl + eni/map files with index 000..(N-1), e.g. 1 → just 000, 32 → 000..031.
_ENI_COUNT = "ALL"


def test_dash_api_load_speed_pl(localhost, duthost, dpuhosts, dpu_index, config_facts, creds):
    """Render DASH configs to a temp dir then push via gnmi_client.py; record per-file load time."""
    dpuhost = dpuhosts[dpu_index]

    # Pre-flight: SSH port check (ping/midplane-status unreliable after route removal).
    dpu_name = f"DPU{dpuhost.dpu_index}"
    dpu_midplane_ip = "169.254.200.%d" % (dpuhost.dpu_index + 1)
    logger.info("Pre-flight: assuming %s is up at %s (no automated check)", dpu_name, dpu_midplane_ip)

    # Render configs under the repo so the host docker daemon can bind-mount them (/tmp isn't shared).
    render_output_dir = tempfile.mkdtemp(prefix="dash_cfg_", dir=os.path.dirname(os.path.abspath(__file__)))
    logger.info("Rendering DASH configs into %s", render_output_dir)
    render.generate(dict(render.DEFAULTS), render_output_dir, prefix="pl_100")

    config_dir = os.path.join(render_output_dir, f"dpu{dpuhost.dpu_index}")
    assert os.path.isdir(config_dir), f"Config directory not found after render: {config_dir}"

    pattern = f"*dpu{dpuhost.dpu_index}*.json"
    files = sorted(f for f in os.listdir(config_dir) if fnmatch.fnmatch(f, pattern) and f.endswith(".json"))
    assert files, f"No JSON config files found matching '{pattern}' in {config_dir}"

    # Filter by _ENI_COUNT: keep files whose 3-digit index is < N; "ALL" keeps everything.
    if _ENI_COUNT != "ALL":
        n = int(_ENI_COUNT)
        filtered = []
        for f in files:
            m = re.search(r"\.(\d{3})(apl|eni|map)\.json$", f)
            if m and int(m.group(1)) < n:
                filtered.append(f)
        assert filtered, f"_ENI_COUNT={_ENI_COUNT} filtered out all files (had {len(files)})"
        logger.info("_ENI_COUNT=%s: pushing %d/%d rendered files", _ENI_COUNT, len(filtered), len(files))
        files = filtered

    logger.info("Rendered %d config files to load for dpu%d", len(files), dpuhost.dpu_index)

    # ── Derive DPU IPs based on hwsku ──────────────────────────────────────
    hwsku = duthost.facts.get("hwsku", "")
    logger.info("NPU hwsku: %s", hwsku)

    if "Cisco" in hwsku:
        dpu_dataplane_ip = "18.%d.202.1" % dpuhost.dpu_index
    else:
        dpu_dataplane_ip = "10.0.0.%d" % (57 + dpuhost.dpu_index * 2)
    logger.info("DPU%d dataplane IP: %s", dpuhost.dpu_index, dpu_dataplane_ip)

    mem_before = {
        "NPU": _collect_memory(duthost),
        "DPU": _collect_memory(dpuhost),
    }
    redis_before = _collect_redis_memory(dpuhost)

    dpu_pre_config(dpuhost)
    npu_pre_config(duthost, dpu_midplane_ip, dpu_dataplane_ip)

    timings = {}
    mem_timeline = []
    total_start = time.time()

    try:
        load_json_via_gnmi(localhost, duthost, dpuhost, config_facts, config_dir, files, timings,
                           creds, mem_timeline)
    finally:
        shutil.rmtree(render_output_dir, ignore_errors=True)
        logger.info("Cleaned up rendered config dir: %s", render_output_dir)

        # Always print results, even if the load raised an exception.
        total_elapsed = time.time() - total_start
        try:
            mem_after = {
                "NPU": _collect_memory(duthost),
                "DPU": _collect_memory(dpuhost),
            }
            redis_after = _collect_redis_memory(dpuhost)
            _print_results(timings, total_elapsed, mem_before, mem_after, redis_before, redis_after, mem_timeline)
        except Exception:
            logger.exception("Failed to collect/print post-test results")

    # Check DPU alive via dataplane ping (midplane reachability unreliable after route removal).
    midplane_out = duthost.show_and_parse("show chassis module midplane-status")
    dpu_row = next((r for r in midplane_out if r.get("name", "").strip().upper() == dpu_name), None)
    midplane_reachability = dpu_row.get("reachability", "").strip() if dpu_row else "unknown"
    logger.info("%s midplane reachability after push: %s (expected False — midplane route removed)", dpu_name, midplane_reachability)

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

    # Verify ENIs propagated from APPL_DB into COUNTERS_ENI_NAME_MAP (takes time).
    _ENI_EXPECTED = (len(files) - 1) // 2  # 2 files per ENI (eni, map) + apl per DPU
    if _ENI_EXPECTED < 1:
        _ENI_EXPECTED = 1
    _ENI_POLL_INTERVAL = 4   # seconds between polls
    _ENI_TIMEOUT = 15        # 15 seconds total
    logger.info("DPU: waiting for %d ENIs in COUNTERS_ENI_NAME_MAP (timeout %ds)...", _ENI_EXPECTED, _ENI_TIMEOUT)
    deadline = time.time() + _ENI_TIMEOUT
    eni_count = 0
    while time.time() < deadline:
        eni_out = dpuhost.shell('sonic-db-cli COUNTERS_DB HGETALL "COUNTERS_ENI_NAME_MAP"', module_ignore_errors=True)
        eni_stdout = eni_out.get("stdout", "")
        # sonic-db-cli returns a Python-repr dict; count keys via 'eni-' occurrences.
        eni_count = eni_stdout.count("eni-")
        logger.info("DPU: ENIs found: %d / %d", eni_count, _ENI_EXPECTED)
        logger.info("DPU: COUNTERS_ENI_NAME_MAP raw output:\n%s", eni_stdout or "(empty)")
        if eni_count >= _ENI_EXPECTED:
            break
        time.sleep(_ENI_POLL_INTERVAL)
    assert eni_count >= _ENI_EXPECTED, \
        "Expected %d ENIs in COUNTERS_ENI_NAME_MAP but found %d after %ds" % (_ENI_EXPECTED, eni_count, _ENI_TIMEOUT)
