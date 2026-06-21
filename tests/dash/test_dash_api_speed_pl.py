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
_ENI_COUNT = 1


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
    # 1 ENI with the full private-link per-ENI scale on DPU0: 64 000 VNet mappings
    # plus ~500 outbound routes, isolated to a single ENI. MINIMAL_SINGLE_ENTRY
    # stays False so the map template emits ACL_NSG_COUNT*2 * ACL_RULES_NSG/2 =
    # 10 * 6400 = 64 000 mappings; ENI_COUNT=1 / DPUS=1 generates exactly one ENI,
    # and TOTAL_OUTBOUND_ROUTES caps that ENI's route table at ~500 (per-ENI scale,
    # not the 128k full-table figure that ENI_COUNT=1 would otherwise imply).
    render_params = dict(render.DEFAULTS)
    render_params["DPUS"] = 1
    render_params["ENI_COUNT"] = 1
    render_params["MINIMAL_SINGLE_ENTRY"] = False
    render_params["TOTAL_OUTBOUND_ROUTES"] = 500
    render.generate(render_params, render_output_dir, prefix="pl_100")

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

    dpu_pre_config(dpuhost, dpu_dataplane_ip)
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
    _ENI_POLL_INTERVAL = 5   # seconds between polls
    _ENI_TIMEOUT = 120       # 2 minutes (64k mappings can keep syncd busy)
    # COUNTERS_ENI_NAME_MAP is the historical signal but does NOT populate on the
    # Cisco/Pensando DPU (the flex-counter ENI map stays empty there even when the
    # ENI is fully programmed). The authoritative cross-platform signal is the SAI
    # ENI object count in ASIC_DB, so gate on max(COUNTERS, ASIC_DB).
    logger.info("DPU: waiting for %d ENIs programmed in hardware (timeout %ds)...", _ENI_EXPECTED, _ENI_TIMEOUT)
    deadline = time.time() + _ENI_TIMEOUT
    eni_count = 0
    while time.time() < deadline:
        cmap = dpuhost.shell('sonic-db-cli COUNTERS_DB HGETALL "COUNTERS_ENI_NAME_MAP"',
                             module_ignore_errors=True).get("stdout", "")
        # sonic-db-cli returns a Python-repr dict; count keys via 'eni-' occurrences.
        counters_count = cmap.count("eni-")
        asic_out = dpuhost.shell("sonic-db-cli ASIC_DB KEYS 'ASIC_STATE:SAI_OBJECT_TYPE_ENI:*' | wc -l",
                                 module_ignore_errors=True).get("stdout", "").strip()
        try:
            asic_count = int(asic_out or "0")
        except ValueError:
            asic_count = 0
        eni_count = max(counters_count, asic_count)
        logger.info("DPU: ENIs found: %d / %d (COUNTERS_ENI_NAME_MAP=%d, ASIC_DB ENI=%d)",
                    eni_count, _ENI_EXPECTED, counters_count, asic_count)
        if eni_count >= _ENI_EXPECTED:
            break
        time.sleep(_ENI_POLL_INTERVAL)
    assert eni_count >= _ENI_EXPECTED, \
        "Expected %d ENIs programmed (COUNTERS_ENI_NAME_MAP or ASIC_DB) but found %d after %ds" % (
            _ENI_EXPECTED, eni_count, _ENI_TIMEOUT)
