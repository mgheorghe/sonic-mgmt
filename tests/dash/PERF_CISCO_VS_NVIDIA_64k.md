# DASH API load speed — Cisco vs Nvidia (1 ENI × 64k mappings)

Single-ENI private-link push via `test_dash_api_speed_pl.py`
(`gnmi_agent_extracted/gnmi_client.py`, `--batch_val 3000`). One ENI rendered at
full private-link per-ENI scale: **64 000 VNet mappings + ~360 outbound routes**
(`pl_100`, 64001 ops in the map file).

## Cisco vs Nvidia — 1 ENI × 64k mappings push

| File (64001 ops total) | Cisco DPU0 (today) | Nvidia DPU1 (2026‑06‑10) |
|---|---|---|
| `000apl` (appliance) | 1.26s | 0.30s |
| `000eni` (ENI + routes) | 1.30s | 0.29s |
| `000map` (64000 mappings) | 8.57s | 11.02s |
| Total 1‑ENI push | 11.1s | 11.6s |
| DPU_APPL_DB DBSIZE | 64367 | 64008 |
| ENI verify | ASIC_DB ENI=1 (COUNTERS empty) | COUNTERS_ENI_NAME_MAP=1 |

## Takeaways

- **Total is essentially the same (~11s)** for the full 1‑ENI / 64k‑mapping config.
- On the **bulk 64k‑mapping file, Cisco is ~22% faster** (8.57s vs 11.02s) — the
  platform/SAI‑throughput‑relevant number.
- On the **small files, Nvidia is much faster** (~0.3s vs ~1.3s). That gap is
  almost certainly **transport, not platform**: the Cisco run used **mTLS** (a TLS
  handshake per `gnmi_client.py` invocation ≈ +1s/file), while the saved Nvidia run
  was **plaintext (`--noTLS`)**. The big file amortizes the handshake; the tiny
  files are dominated by it.
- **Verification differs by platform:** Nvidia/BlueField populates
  `COUNTERS_ENI_NAME_MAP`; the Cisco/Pensando DPU leaves it empty even when the ENI
  is fully programmed, so the test verifies via
  `ASIC_DB ASIC_STATE:SAI_OBJECT_TYPE_ENI:*` (max of the two signals).

## Caveats (not perfectly apples‑to‑apples)

- Different transport: Cisco **mTLS :50051** vs Nvidia **plaintext :50052** (the
  2026‑06‑10 Nvidia run predates the 50051 standardization).
- Different DPU index: Cisco **DPU0** vs Nvidia **DPU1**.
- A strictly identical re‑run (both mTLS :50051, both DPU0) requires recovering
  Nvidia DPU0 (its midplane was down at measurement time — reboot the NPU per the
  standard recovery, then re‑run).

## Sources

- Cisco: `tests/dash/work/cisco_64k_pass_20260621_101900/` — `test_run.log`,
  `npu_varlog.tgz`, `dpu0_varlog.tgz`. NPU `keysight-css01` (10.36.77.121),
  Cisco 8102‑28FH‑DPU‑O, Pensando DPU0, gNMI 50051 mTLS, image `internal.167426684`.
- Nvidia: `tests/dash/work/nss_dpu1_load_OK_20260610_185204/LOAD_SUMMARY.txt`.
  NPU `keysight-nss01` (10.36.78.150), BlueField‑3 DPU1, gNMI 50052 plaintext.

## How to reproduce

```bash
cd /home/dash/sonic-mgmt/sonic-mgmt/tests && \
  ANSIBLE_LIBRARY=../ansible/library ANSIBLE_MODULE_UTILS=../ansible/module_utils \
  ANSIBLE_HOST_KEY_CHECKING=False \
  pytest dash/test_dash_api_speed_pl.py \
    --testbed=keysight-css01 --testbed_file=../ansible/testbed.yaml \
    --inventory=../ansible/lab --host-pattern=keysight-css01 \
    --dpu_index=0 --dpu-pattern=keysight-css01-dpu0 --cache-clear -v
```

(Nvidia: swap `css01`→`nss01`.) The render knobs (1 ENI, 64k mappings, ~500‑route
cap) live at the top of `test_dash_api_speed_pl.py::test_dash_api_load_speed_pl`.
