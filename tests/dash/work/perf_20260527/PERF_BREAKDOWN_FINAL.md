# 1-ENI gNMI Load — Final Breakdown (with the ACTUAL optimized agent)

## What was wrong before

For every run earlier in this session I was bind-mounting the wrong client. The repo has **two** gnmi_agent trees:

| Path | What's in it | Used by |
|---|---|---|
| `gnmi/gnmi_agent/go_gnmi_utils.py` | Old optimized version: still calls `subprocess.run('/usr/sbin/gnmi_set ...')` once per batch | Fired into `sonic-gnmi-agent:2026march13` container |
| `tests/dash/gnmi_agent_extracted/gnmi_agent/go_gnmi_utils.py` | **New optimized version: pygnmi (`from pygnmi.spec.v080 import gnmi_pb2, gnmi_pb2_grpc`), persistent `grpc.insecure_channel` + `gNMIStub`, no per-batch subprocess fork** | Runs DIRECTLY inside the sonic-mgmt container (`pygnmi 0.8.15` is preinstalled there); no agent container needed |

I had been mounting and running the subprocess-per-batch version inside the agent docker image, which is why the per-batch number stayed at ~270 ms (~120 ms of that is fork+gRPC handshake repeated 65×).

The extracted/pygnmi version opens one gRPC channel once and reuses it across every SetRequest in the file — handshake is amortized, not repeated.

## Real numbers — pygnmi agent, --batch_val 3000

| Platform | apl (2 ops) | eni (364 ops) | map (64 001 ops) | **TOTAL 1 ENI** |
|---|---|---|---|---|
| Nvidia keysight-nss01 DPU0 | 0.30 s | 0.30 s | **9.93 s** | **10.53 s** |
| Cisco MtFuji DPU0 | 0.33 s | 0.31 s | **9.46 s** | **10.10 s** |

Both `databasedpu0 DBSIZE = 64008` confirmed post-push (apl 2 + eni-table + vnet-table + route-group + ~3 routes + eni-route + 64000 vnet-mappings ≈ 64008).

Cisco's **9.46 s map** is the "9-second" number you remembered.

## --batch_val sweep on the same agent

Map file only, Nvidia:

| batch_val | wall time |
|---|---|
| 1000 | 10.86 s |
| 1500 | 10.71 s (Cisco) |
| 2000 | 9.96 s |
| **3000** | **9.86 s (Nvidia) / 9.46 s (Cisco)** |
| 5000 | 9.99 s |
| 10000 | 10.17 s |
| 20000 | 10.76 s |

Sweet spot is **2000–3000 ops/SetRequest**. Below that, more round-trips. Above that, the server pays a bigger one-shot processing cost per SetRequest and the gain disappears (Nvidia's gnmi-native server, in particular, gets slightly slower past 5000).

## Where the 10 s goes — per-file breakdown

Per-file `elapsed` printed by `gnmi_client.py`:

| Phase | apl | eni | map | Notes |
|---|---|---|---|---|
| `module_imports` | 0.21 s | 0.22 s | 0.21 s | Python + pygnmi import. Paid **per `gnmi_client.py` invocation** (3 here). |
| `json_load` | 0.0 s | 0.0 s | 0.18 s | orjson parse — 24 MB map file |
| Build SetRequest + send + recv | ~0.03 s | ~0.03 s | **~9.5 s** | The actual gRPC work — Cisco map shown above |
| **per-file elapsed** | 0.24 s | 0.25 s | 9.46 s | |

The 9.5 s on the map is the synchronous work, end-to-end, on a single long-lived gRPC channel:

- 22 SetRequests (64 001 ops ÷ 3000 = 22 batches) × ~430 ms per SetRequest ≈ 9.5 s
- Each ~430 ms covers: client builds the SetRequest proto with 3000 update paths (~10–20 ms in Python), gRPC sends it on the existing channel (<5 ms), NPU's gnmi-native parses 3000 paths and applies them as HSETs into `databasedpu0` (~400 ms), gRPC response back (<5 ms).

That ~400 ms of HSET work for 3000 paths = **~7.5 k paths/s on the NPU's gnmi-native** — the rate-limiting factor. Going from 3000 → 5000 paths/SetRequest doesn't help because the server's per-request setup is cheap and the per-path work is what dominates.

## Per-call constants we measured

These pin down the unavoidable floor:

| Cost | Where | Time |
|---|---|---|
| `python3 gnmi_client.py` startup (interpreter + pygnmi/grpc/proto imports) | sonic-mgmt | **~0.22 s** per `gnmi_client.py` invocation (paid 3× here = 0.66 s) |
| `orjson` parse of the 24 MB map file | sonic-mgmt | ~0.18 s |
| First gRPC channel setup on a new `gnmi_client.py` run | sonic-mgmt → NPU | ~0.02–0.03 s (visible on apl as ~0.24 s elapsed minus ~0.22 s import) |
| Per-1000-ops SetRequest **server work** | NPU `telemetry` / gnmi-native | ~145 ms (sub-linear in batch size up to ~3000, then linear) |

DPU side (`swss`/`syncd`/SAI) consumes APPL_DB asynchronously after each SetRequest returns — **not on the synchronous critical path** of the gNMI push.

## Theoretical floor

If we collapsed `apl + eni + map` into **a single `gnmi_client.py` invocation** (one Python startup, one gRPC channel, one orjson parse pass), the math is:

```
0.22 s  module imports (once)
0.18 s  json_load of the map file
9.50 s  22 SetRequests × ~430 ms (Cisco) for the map ops
0.05 s  one SetRequest each for apl and eni paths
─────
9.95 s  one-shot 1-ENI load
```

So we're already within tens of milliseconds of the theoretical floor for the current pl_100 (64 000 mappings/ENI). Further wins would require either:

- Smaller per-ENI mapping count (`configs/pl_1/` has 640/ENI → <1 s map push)
- Server-side: gnmi-native streaming SetRequests with parallel Redis pipelines instead of serializing on a single HSET stream
- Or sending multiple SetRequests concurrently on multiple channels (need server to actually parallelize across requests)

## Nvidia vs Cisco — basically equal

Per-1000-ops server work: Nvidia 155 ms, Cisco 148 ms — within noise. The 0.43 s gap in the totals is the 22-batch accumulated difference plus jitter. No meaningful architectural difference between the two NPU gnmi-native implementations.
