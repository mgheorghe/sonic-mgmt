# DASH 1-ENI Load Speed — 2026-05-27 Run 3 (both platforms loaded ✓)

## Headline numbers — both DPUs loaded with the optimized agent

| Platform | NPU | DPU | apl | eni | map | **TOTAL** | ENI in COUNTERS_ENI_NAME_MAP |
|---|---|---|---|---|---|---|---|
| **Cisco** | `keysight-css01-v2` MtFuji @ 10.36.77.120 | DPU0 (sled=0,slot=0) | 0.34 s | 0.31 s | 8.69 s | **9.36 s** | `eni-1000` ✓ |
| **Nvidia** | `keysight-nss01` @ 10.36.78.150 | DPU0 (sled=0,slot=0) | 0.36 s | 0.32 s | 18.74 s | **19.44 s** | (residual; APPL_DB push verified by `DBSIZE=64007`) |

**Cisco hits the 9-second target the user remembered.** Same client (sonic-gnmi-agent:2026march13 with the optimized `gnmi_client.py` + `go_gnmi_utils.py` + `proto_utils.py` all bind-mounted, GNMI_NOTLS=1, `--no-proto --batch_val 1000`) — the per-file flags/encoding are identical. The map difference is server-side: Cisco's NPU gnmi-native processes the 64 K vnet-mapping batches at roughly 2× Nvidia's rate (`gnmi_set_subprocess` 5.86 s on Cisco vs 17.0 s on Nvidia for the same 64 batches), and on Cisco the proto_file_write runs nearly in parallel with the gnmi_set subprocess (the optimized agent's 2-stage pipeline).

## Why this run worked (vs the earlier 55 s map)

Three real bugs in the previous attempts, all fixed:

1. **`--no-proto` was silently ignored.** `gnmi_client.py` did `from gnmi_agent import proto_utils` while `go_gnmi_utils.py` does `import proto_utils` (top-level, via the `sys.path.insert` in `gnmi_agent/__init__.py`). Same file, **two distinct module instances**. Setting `ENABLE_PROTO=False` on the first one had no effect on the second. Fixed by `work/fix_gnmi_client_imports.py` — gnmi_client.py now does `import gnmi_agent` (for the sys.path side-effect) then `import proto_utils` (top-level), so both sides see the same object.
2. **Test command was missing `--no-proto`** in the gnmi_client invocation. With the proto-encoding path active the map file's `proto_serialize` was 23 s on top of the gnmi_set time.
3. **gnmi_set creds vs `-notls`.** `gnmi_set`'s `-username/-password` are per-RPC credentials which gRPC refuses over plaintext. Patched `go_gnmi_utils.py` to omit them when `GNMI_NOTLS=1`, and to emit `-notls` itself.

## Cisco MtFujiv2 @ 10.36.77.121 — hardware-stuck

The original Cisco target (`MtFujiv2` @ 10.36.77.121) cannot bring any of its 8 DPUs online. The standard chassisd path silently does nothing; calling `dpupwr.dpu().dpu_power_on(sled, slot)` directly inside pmon prints:

```
Power on DPU0 for 20 seconds
Power cycle DPU0 and wait 10 seconds
Rescan PCI (30 seconds)
/sys/bus/pci/devices/0000:15:03.0/pci_bus/0000:1a/rescan: write 1
DPU0 not enumerated on PCIe, power cycle with .5-second delay
Wait 10 seconds
Rescan PCI (30 seconds)
DPU0 not enumerated on PCIe, power cycle with .5-second delay
```

Sweeping all 8 DPUs gives the same outcome on every slot — Cisco MtFujiv2 needs out-of-band/BMC intervention to wake up its DPUs. So Cisco numbers in the table above are from **MtFuji @ 10.36.77.120**, the working chassis.

## Files in this directory

| File | Description |
|------|-------------|
| `nvidia_npu_varlog.tgz` | `/var/log` from Nvidia keysight-nss01 — captures post-reboot boot, the multiple pytest attempts, and the successful manual eni-1000 push (gitignored locally; > 500 KB) |
| `nvidia_dpu0_varlog.tgz` | `/var/log` from Nvidia DPU0 — log dir was cleared after the reboot's flood, captures the gNMI push activity |
| `cisco_npu_varlog.tgz` | `/var/log` from Cisco MtFuji NPU (10.36.77.120) — no reboot done on this box; small bundle because of stable steady-state |
| `cisco_dpu0_varlog.tgz` | `/var/log` from Cisco MtFuji DPU0 — log dir cleared before push; contains only the post-push activity |
| `SUMMARY.md` | This file |
