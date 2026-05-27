#!/usr/bin/env python3
"""Run inside the Cisco NPU's pmon container to force-power-on DPU0.

The standard `config chassis module startup DPU0` path queues a power-up
request into chassisd's operation_queue but on this platform DPU0 is
stuck (admin=up, oper=Offline, midplane unreachable, no Pensando PCIe
device visible to lspci). chassisd reports recurring
'?Unable to resolve [hwmon:bmc]/device/fanN_presence' which suggests the
BMC link the platform driver uses is partly broken.

This script bypasses chassisd and calls the lowest-level dpu_power_on()
directly so we can see the actual exception (if any) the platform layer
raises when trying to power the DPU.
"""

import logging
import time

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")

try:
    import dpupwr
except Exception as e:
    print(f"FAILED to import dpupwr: {e!r}")
    raise SystemExit(2)

print(f"dpupwr module: {dpupwr.__file__}")
d = dpupwr.dpu()

# Cisco numbering: sled = dpu_id // DPUS_PER_SLED, slot = dpu_id % DPUS_PER_SLED.
# For DPU0 both are 0.
print("attempting dpu_power_off(0, 0) ...")
try:
    print(" =", d.dpu_power_off(0, 0))
except Exception as e:
    print(f" raised {e!r}")

time.sleep(3)

print("attempting dpu_power_on(0, 0) ...")
try:
    print(" =", d.dpu_power_on(0, 0))
except Exception as e:
    print(f" raised {e!r}")

# Show whatever attribute it exposes
attrs = [a for a in dir(d) if not a.startswith("_")]
print(f"dpupwr.dpu() exposes: {attrs}")
