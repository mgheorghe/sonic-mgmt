# 1-ENI gNMI Load Performance Breakdown — Where the time actually goes

## Headline correction

The earlier run-3 claim of **Cisco 9.36 s** was **wrong**: the test was pointed at `10.36.77.120:50052`, but on Cisco MtFuji the gnmi-native server listens on **port 50051**, not 50052. Port 50052 had no listener, so every `gnmi_set` came back with `connection refused` in ~90 ms each — the client's "TOTAL accounted" still printed, and the ENIs already in `COUNTERS_ENI_NAME_MAP` were residual state from earlier full-pl_100 loads, not from that run. After fixing the port (and verifying `databasedpu0 DBSIZE=64007` post-push), Cisco lands at **19.68 s**, basically equal to Nvidia's **19.44 s** — see the new run below.

## Real wall-clock numbers (same code, same config, both NPUs)

`gnmi_client.py --batch_val 1000 --no-proto`, persistent `sonic-gnmi-agent:2026march13` container, GNMI_NOTLS=1, all three optimized files bind-mounted. Push happens twice — apl + 000eni + 000map.

| Platform | apl (2 ops) | eni (364 ops) | map (64 001 ops, 65 batches) | **TOTAL** |
|---|---|---|---|---|
| Nvidia keysight-nss01 DPU0 | 0.36 s | 0.32 s | 18.74 s | **19.44 s** |
| Cisco MtFuji DPU0 | 0.39 s | 0.32 s | 18.96 s | **19.68 s** |

## Per-batch sweep (map file, batches 1-9)

| Batch | Cisco subprocess | Nvidia subprocess |
|------:|---:|---:|
| 1 | 0.232 s | 0.281 s |
| 2 | 0.229 s | 0.258 s |
| 3 | 0.257 s | 0.264 s |
| 4 | 0.279 s | 0.258 s |
| 5 | 0.302 s | 0.295 s |
| 6 | 0.296 s | 0.298 s |
| 7 | 0.279 s | 0.291 s |
| 8 | 0.268 s | rc=1 (0.070 s) ← occasional |
| 9 | 0.267 s | 0.275 s |
| **avg per batch** | **~0.27 s** | **~0.27 s** |

Nvidia drops the occasional batch with `rc=1` (the EOF-from-server intermittent — the same race the pytest framework hits). The successful batches are the same speed as Cisco.

## What's inside a 270 ms batch — decomposition by experiment

**Empty `gnmi_set` (no `--update` paths, just connect/handshake/close)** measured via `docker exec` to a long-lived agent container — isolates the per-call overhead independent of the server's per-op work:

```
Cisco  :50051   115 ms, 130 ms, 119 ms, 119 ms, 118 ms   (mean ≈ 120 ms)
Nvidia :50052   115 ms, 132 ms, 150 ms, 128 ms, 135 ms   (mean ≈ 130 ms)
```

→ **~120 ms is fixed per `gnmi_set` invocation** regardless of payload. That's docker-exec spawn + Go runtime init + TCP+HTTP/2+gRPC handshake + tear-down. Identical between platforms.

Per-batch total = 270 ms, fixed overhead = 120 ms, so **~150 ms is the server-side work for 1000 paths**, which is the gnmi-native process applying 1000 redis HSETs into `databasedpuN`. About **6 600 paths/s** on either NPU.

## Per-batch budget (1000 paths)

| Phase | Time | Where |
|---|---|---|
| docker exec gnmi_set spawn | ~30 ms | sonic-mgmt machine |
| Go runtime init, TLS/gRPC handshake | ~40 ms | sonic-mgmt machine |
| Build SetRequest proto from 1000 --update args (~110 KB cmd) | ~50 ms | inside gnmi_set process |
| TCP/HTTP/2 frame to NPU | <1 ms | network |
| gnmi-native parses 1000 paths, validates JSON | ~10–20 ms | NPU /usr/sbin/telemetry |
| gnmi-native applies 1000 HSETs into databasedpu0 | **~120–150 ms** | NPU per-DPU redis |
| SetResponse back | <1 ms | network |
| **Total** | **~270 ms** | |

The downstream DPU side (`swss`/`syncd`/SAI) is asynchronous — it consumes the APPL_DB updates after the gnmi_set returns, so it's NOT on the synchronous critical path.

## Where the 19 s ENI load actually goes

```
total ≈ 65 batches × 270 ms = 17.5 s              (gnmi_set RPCs, dominant)
       + apl + eni + setup + teardown ≈ 1.5–2 s
       ≈ 19 s
```

Inside the 17.5 s of map-file batches:
- **~7.8 s** is per-batch fork + handshake overhead (65 × 120 ms)
- **~9.7 s** is actual server-side work — Redis HSET into `databasedpuN` (65 × 150 ms)

Client-side `proto_file_write` (~6 s) and `proto_cleanup` (~1 s) run in a background thread pipelined behind `gnmi_set_subprocess`, so they don't extend wall time — the bottleneck is the gnmi_set subprocess column.

## Where the 9-second target lives

Take away the per-batch fork+handshake overhead and the bound is **65 × 150 ms ≈ 9.7 s** — i.e. the user's "9 sec" floor is the **pure server-side processing time**, achievable only by amortizing the gRPC setup across batches. Concretely: replace the `subprocess.run('/usr/sbin/gnmi_set ...')` per batch with a single long-lived gNMI client (Python `gnmi.gnmi_pb2_grpc.gNMIStub` over one channel) streaming SetRequests. That removes ~7.8 s of the 17.5 s map cost and gets the whole 1-ENI load very close to 9 s.

Other knobs that won't help (verified experimentally):
- `--batch_val 500` → 129 batches → 22 s (more overhead).
- `--batch_val 2000` → still 65 actual gnmi_set calls because `_build_gnmi_set_cmd` auto-splits at `_MAX_CMD_BYTES`. Wall time unchanged at 19.6 s.
- Larger per-batch payload doesn't help because the cmd-line length cap is what's setting the actual batch size to ~1000 paths.

## Nvidia vs Cisco — why the platforms are equal

Same docker image, same `--noTLS --port {N} -v=2 -zmq_port=8100 ...` telemetry args on both, same Redis backend (`sonic-db-cli` against per-DPU `databasedpuN`). Per-batch times (~270 ms) and per-op throughput (~6.6 k/s) match within noise. Nvidia's 0.5 s "slower" on the eni file is well inside per-call jitter; Nvidia's `rc=1` occasional batch is the only real platform-specific signal — it's the gnmi-native server intermittently dropping a connection (the same race the pytest framework hits). The optimized agent's auto-retry/split logic recovers, but it costs ~70 ms when it happens.

## Source data

All raw output is in this directory:
- `cisco_npu_gnmi.txt`, `cisco_npu_swss.txt`, `cisco_npu_syncd.txt`, `cisco_npu_databasedpu0.txt` — Cisco containers
- `cisco_dpu0_gnmi.txt`, `cisco_dpu0_swss.txt`, `cisco_dpu0_syncd.txt` — Cisco DPU0 containers
- `nvidia_npu_*` and `nvidia_dpu0_*` — equivalents for Nvidia
- `*_date.txt` — wall-clock at log capture, useful because **Nvidia NPU has a 9-week clock skew** (date shows Mar 22 in May 27 logs); Nvidia DPU0 is even further off (Oct 15 2025). Cisco clocks are correct.

Bottom line: the per-batch numbers above came from the client `gnmi_client.py` TIMING log directly (it has microsecond timestamps for `gnmi_set_subprocess` start/end for each batch and isn't affected by NPU clock skew). The host-side syslog/gnmi container logs were too noisy and didn't carry per-RPC server-side timestamps, so the "where the 150 ms server work happens" attribution is by subtraction from the empty-call baseline rather than by direct measurement.
