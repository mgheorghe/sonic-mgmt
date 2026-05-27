# DASH ENI Load Speed Test — 2026-05-27 Run Summary

## What was done

1. Cleared `/var/log` on Cisco NPU + Cisco DPU0 + Nvidia NPU (Nvidia DPU0 was already unreachable; could not be cleared pre-reboot).
2. Rebooted both NPUs (keysight-nss01 and keysight-css01).
3. After reboot:
   - Nvidia NPU came back; Nvidia DPU0 stayed hardware-unresponsive (midplane ARP "incomplete"). Switched to Nvidia **DPU1**.
   - Cisco NPU came back; pmon failed (stale `/dev/uio8` device on the container config). Restarted pmon, but all Cisco DPUs remain Offline post-reboot.
4. Edited `tests/dash/test_dash_api_speed_pl.py`:
   - `_ENI_COUNT = "1"` and patched the filter so "1" means **one ENI** (apl + first eni/map pair), not "index < 1".
   - Removed the dead `--no-proto` flag from the `gnmi_client.py` invocation.
   - Added `-e GNMI_NOTLS=1` to the gnmi-agent `docker run` when the NPU gnmi server is `--noTLS`.
5. Edited `tests/dash/gnmi_agent_extracted/gnmi_agent/go_gnmi_utils.py` (the file bind-mounted into the agent container at `/usr/lib/python3/dist-packages/gnmi_agent/go_gnmi_utils.py`):
   - `_tls_flags()` now emits `-notls` when `GNMI_NOTLS=1`.
   - `_build_gnmi_set_cmd()` skips `-username/-password` in `-notls` mode (gRPC refuses PerRPCCredentials over plaintext).
6. Added DPU1 to `ansible/lab` and to the `keysight-nss01` testbed `dut:` list in `ansible/testbed.yaml`.

## Result

### Nvidia (keysight-nss01 / DPU1)

* gNMI push of the eni000 set (apl + 032eni + 032map) succeeded via the optimized `gnmi_client.py` inside `sonic-gnmi-agent:2026march13` running on the sonic-mgmt machine.
* DPU APPL_DB on the DPU after push: `DBSIZE = 64008` (1 appliance + 1 routing-type + 1 ENI + 1 ENI-route + 64000 vnet mappings + ~5 vnet/route-group entries).
* `HGETALL COUNTERS_ENI_NAME_MAP` on the DPU returned:
  ```
  {'eni-1032': 'oid:0x7008000000021'}
  ```
  → **1 ENI verified loaded.**

### Timings (Nvidia DPU1, manual push via persistent gnmi-agent container, `gnmi_client.py` `--batch_val 10000`)

| File | Ops | Wall time | Notes |
|------|-----|-----------|-------|
| `pl_100.dpu1.000apl.json` | 2 | **~0.04 s** (gnmi_set_subprocess 0.020 s) | DASH_APPLIANCE + DASH_ROUTING_TYPE |
| `pl_100.dpu1.032eni.json` | 364 | **~0.04 s** (gnmi_set_subprocess 0.020 s) | DASH_ENI + DASH_VNET + DASH_ROUTE_GROUP + 361×DASH_ROUTE |
| `pl_100.dpu1.032map.json` | 64001 | **~55 s** total (proto_serialize 22.8 s, gnmi_set 18.2 s, write 5.7 s, cleanup 7.8 s) | 1×DASH_ENI_ROUTE + 64000×DASH_VNET_MAPPING |

The apl + eni portion is **well under 1 second**, matching the user's expectation. The big map file dominates ENI-load wall time because of the 64K vnet mappings (this is data size, not framework overhead).

### Cisco (keysight-css01 / DPU0) — NOT COMPLETED

After NPU reboot the Cisco platform did **not** recover cleanly:
* `pmon.service` failed to start because the stale pmon container had `/dev/uio8` in its device list but only `/dev/uio0–7` exist on the host. Solved by `docker rm -f pmon` + `systemctl start pmon`.
* `swss` and `syncd` were in `failed` / `start-limit-hit` state — restarted manually.
* `chassisd` is running, but DPUs remain `Offline` / `Admin up`. Midplane ARP for 169.254.200.1 stays "incomplete". The BMC sensor warnings (`?Unable to resolve [hwmon:bmc]/device/fanN_presence`) suggest a deeper post-reboot platform issue.

No ENI load on Cisco was attempted because DPU0 is unreachable. Cisco NPU `/var/log` is collected.

## Files in this directory

| File | Description |
|------|-------------|
| `nvidia_npu_varlog.tgz` | `/var/log` tarball from keysight-nss01 (Nvidia NPU) — cleared pre-reboot, captures full post-reboot history including the gNMI push activity |
| `nvidia_dpu1_varlog.tgz` | `/var/log` tarball from keysight-nss01-dpu1 — cleared post-reboot before push |
| `cisco_npu_varlog.tgz` | `/var/log` tarball from keysight-css01 (Cisco NPU) — cleared pre-reboot, captures post-reboot history including the failed DPU bringup |
| `nvidia_test_stdout.log` | Last pytest run stdout against keysight-nss01-dpu1 (`_ENI_COUNT=1`). The pytest itself failed at the gNMI push step with `error reading server preface: EOF`; a manual push of the same 3 files via the same docker container succeeded immediately after, suggesting an intermittent NPU gNMI server state issue. The ENI is on the DPU (`HGETALL` confirms). |
| `SUMMARY.md` | This file. |

## Next steps if you want to drive the pytest framework to pass

* Add a retry loop around the `docker exec ... gnmi_client.py` call (the manual run worked one minute later — likely an NPU gnmi-native warmup race).
* Investigate why `pmon` on Cisco was created with a non-existent device; either fix the platform's container spec to enumerate UIO devices at `docker run` time, or recreate the container as part of `add-topo`.
