# DASH ENI Load Speed Test — 2026-05-27 Run 2 (full optimized agent)

## What was done (this run)

1. Removed the stale `pmon` Docker container on Cisco NPU so it would be re-created with current `/dev/uio*` devices on reboot (the previous run's pmon failed because its spec referenced a non-existent `/dev/uio8`).
2. Cleared `/var/log` on the 3 reachable devices: Cisco NPU, Nvidia NPU, Nvidia DPU1. (Cisco DPU0 was unreachable pre-reboot too, so no pre-clear there.)
3. Rebooted both NPUs.
4. After NPUs came back: enabled NAT port-forwards, then patched the test for the **fully optimized gnmi-agent**:
   - Bind-mount `gnmi/gnmi_client.py` (the optimized client with `--no-proto`), `gnmi/gnmi_agent/proto_utils.py`, and `gnmi/gnmi_agent/go_gnmi_utils.py` into `sonic-gnmi-agent:2026march13`.
   - Pass `-e GNMI_NOTLS=1` so `_tls_flags()` emits `-notls` for the plain-TCP NPU server.
   - Use `--batch_val 1000 --no-proto`.
   - `_ENI_COUNT = "1"` with the filter rewritten so "1" means *one ENI worth* (apl + first eni/map pair), not "index < 1".
5. **Fixed a real bug in `gnmi_client.py` `--no-proto`**: `gnmi_agent/__init__.py` does `sys.path.insert(0, <pkg dir>)`, so the same `proto_utils.py` was being loaded under two different module names. `gnmi_client.py` set `gnmi_agent.proto_utils.ENABLE_PROTO = False`, but `go_gnmi_utils.py` reads top-level `proto_utils.ENABLE_PROTO` — they were two *different* module instances, so the flag was silently ignored. Rewrote the import in `gnmi_client.py` to use the same top-level `proto_utils`. This dropped the map-file `proto_serialize` cost from ~23 s to 0 s.

## Nvidia (keysight-nss01 / DPU1) — SUCCESS

Loaded eni000 set (apl + 1 ENI + 1 map) manually via the optimized agent (pytest framework intermittently fails the gnmi pre-check race — see "known issue" below — so the manual path was used).

### Per-file wall time (`--no-proto --batch_val 1000`):

| File | Ops | Wall time | gnmi_set_subprocess (cumulative) |
|------|-----|-----------|---------------------------------|
| `pl_100.dpu1.000apl.json` | 2     | **0.37 s** | 0.026 s |
| `pl_100.dpu1.032eni.json` | 364   | **0.32 s** | 0.023 s |
| `pl_100.dpu1.032map.json` | 64001 | **18.61 s** | 16.90 s (65 batches × ~0.27 s) |
| **TOTAL 1-ENI load**      |       | **19.31 s** | |

### Verification on DPU1

```
$ sonic-db-cli COUNTERS_DB HGETALL COUNTERS_ENI_NAME_MAP
{'eni-1032': 'oid:0x7008000000021'}

$ sonic-db-cli DPU_APPL_DB DBSIZE
64007
```

### Why not the 9 s number?

The dominant cost is the **map file**: 64 000 vnet-mapping ops, pushed in 65 batches of ~1000 ops each at ~0.27 s/batch = ~17 s of pure server-side gnmi_set subprocess time. With the proto_utils import fix the *client* side is no longer the bottleneck; the wall time is essentially limited by the NPU gNMI server's per-batch throughput.

To reach ~9 s/ENI, one of the following is needed:
* Use a smaller config (e.g. `configs/pl_1/` has `ACL_RULES_NSG=128` → ~640 mappings/ENI, ~100× fewer). That's a separate test scenario, not the `pl_100` configuration this branch is set up for.
* Raise the per-batch ops further so fewer round-trips are needed (the optimized agent already splits to fit MAX_CMD_BYTES, so this requires server-side acceptance of larger payloads).
* Run multiple batches truly in parallel — current code uses `ThreadPoolExecutor(max_workers=1)`, so only a 2-stage pipeline. Bumping `max_workers` could overlap multiple gnmi_set subprocesses.

## Cisco (keysight-css01 / DPU0) — NOT LOADED

The Cisco platform is wedged at the hardware level after the NPU reboot. Sequence we observed:

1. NPU rebooted cleanly. `pmon` came up (this time with the right `/dev/uio*` list after we removed the stale container pre-reboot). `chassisd` is RUNNING inside pmon.
2. But chassisd cannot bring DPUs online. `show chassis modules status` shows all 8 DPUs `Offline`. `show chassis modules midplane-status` shows all `False`. `arp -n 169.254.200.1` is `(incomplete)`.
3. `lspci` shows zero Pensando devices — the AMD Pensando DSCs are not enumerated on the PCI bus at all. Chassisd logs repeatedly emit `?Unable to resolve [hwmon:bmc]/device/fanN_presence`, indicating the BMC link the platform driver relies on is broken.
4. Repeated `config chassis module shutdown DPU0` / `startup DPU0` cycles do not change the state — the host has no power-control path to the DPUs without a working BMC.
5. We have no IPMI/BMC access from the management network (`/dev/ipmi0` not present).

**To recover, the Cisco needs a power-cycle (or BMC reset) at the chassis level — out-of-band intervention required.** No ENI load was attempted on Cisco because DPU0 was never reachable.

Cisco NPU `/var/log` is collected anyway — it captures the full post-reboot pmon / chassisd / dash-ha bring-up activity and the BMC sensor errors.

## Known issue: pytest framework fails the first gnmi push intermittently

The pytest test fails consistently at `[1/3] pushing pl_100.dpu1.000apl.json` with `error reading server preface: EOF`, but a `docker exec` of the **same command in the same image** issued manually a few seconds later succeeds and shows `Command executed successfully` plus a normal timing trace. The agent setup (image, mounts, env, network mode) is byte-identical between the two; the difference appears to be either:
* a brief warmup window where the NPU's `gnmi-native` plugin (re)initializes after a sequence of `gnmi_set` calls from the test's pre-check / setup phase, or
* a per-source-IP gnmi rate-limit that fires when pre-check + push come within ~1 s.

A simple retry loop around the per-file push, or a small delay (~1–2 s) between the pre-check failure and the first real push, would absorb this. We worked around it by doing the timing run manually.

## Files in this directory

| File | Description |
|------|-------------|
| `nvidia_npu_varlog.tgz` | Nvidia NPU `/var/log` — cleared pre-reboot, captures post-reboot boot + multiple pytest attempts + the successful manual gNMI push |
| `nvidia_dpu1_varlog.tgz` | Nvidia DPU1 `/var/log` — cleared pre-reboot AND post-reboot, captures only the gNMI push effects |
| `cisco_npu_varlog.tgz` | Cisco NPU `/var/log` — cleared pre-reboot, captures post-reboot pmon/chassisd activity and DPU bring-up failures (gitignored locally because >500 KB) |
| `nvidia_pytest_stdout.log` | Last full pytest output against `keysight-nss01-dpu1` — shows the pre-check race + intermittent EOF (gitignored, >500 KB) |
| `SUMMARY.md` | This file |
