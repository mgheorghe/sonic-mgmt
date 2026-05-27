#!/usr/bin/env python3
"""Force-power each Cisco DPU (0-7) via dpupwr, capture per-DPU outcome.

Used after `config chassis module startup DPU{N}` proves to be a no-op
on keysight-css01 — the standard chassisd path queues a power request
that completes silently while `show chassis modules midplane-status`
still reports all DPUs unreachable.

For each DPU we call dpu_power_off then dpu_power_on directly and
report whether the PCIe rescan finds the device.
"""

import logging
import sys
import time

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")

sys.path.insert(0, "/opt/cisco/bin")

import dpupwr  # noqa: E402

d = dpupwr.dpu()
print(f"dpupwr module: {dpupwr.__file__}")

# Cisco numbering: sled = dpu_id // DPUS_PER_SLED, slot = dpu_id % DPUS_PER_SLED.
# 8 DPUs total, DPUS_PER_SLED is typically 2 on this platform.
DPUS_PER_SLED = 2

for dpu_id in range(8):
    sled = dpu_id // DPUS_PER_SLED
    slot = dpu_id % DPUS_PER_SLED
    print(f"\n=== DPU{dpu_id} (sled={sled}, slot={slot}) ===")
    try:
        d.dpu_power_off(sled, slot)
    except Exception as e:
        print(f"  off raised {e!r}")
    time.sleep(1)
    try:
        d.dpu_power_on(sled, slot)
    except Exception as e:
        print(f"  on raised {e!r}")
