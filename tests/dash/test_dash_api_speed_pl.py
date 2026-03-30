import fnmatch
import logging
import os
import time

import pytest

from gnmi_utils import apply_gnmi_file
from tests.common import config_reload

logger = logging.getLogger(__name__)

pytestmark = [pytest.mark.topology("smartswitch"), pytest.mark.skip_check_dut_health]

CONFIG_DIR = os.path.join(os.path.dirname(__file__), "configs", "private-link-50")


def test_dash_api_load_speed_pl(localhost, duthost, ptfhost, dpuhosts, dpu_index):
    """
    Measure the time to load private-link-50 DASH configs onto a DPU via gNMI.

    Loads all JSON files from configs/private-link-50/dpu{dpu_index}/ in sorted
    order, recording per-file and total load times. Designed to mirror the dpu.py
    manual loading script but integrated into the sonic-mgmt pytest framework.

    Results are logged at INFO level so they appear in the test report.
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
        logger.info("%-40s  %.2fs", filename, elapsed)

        duthost.shell(f"rm -f {dut_path}", module_ignore_errors=True)

    total_elapsed = time.time() - total_start

    logger.info("=" * 60)
    logger.info("Total files loaded : %d", len(files))
    logger.info("Total elapsed time : %.2fs", total_elapsed)
    logger.info("Average per file   : %.2fs", total_elapsed / len(files))

    config_reload(dpuhost, safe_reload=True, yang_validate=False)
