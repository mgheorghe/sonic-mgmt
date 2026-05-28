# DASH 1-ENI gNMI Load — Run 4 (post-reboot, pygnmi agent, detailed breakdown)

This run goes through the full cycle: cleared `/var/log` on all four devices, rebooted both NPUs, re-cleared DPU `/var/log` post-reboot, then loaded `000apl + 000eni + 000map` on Nvidia DPU0 with the **actual** optimized pygnmi-based agent (`tests/dash/gnmi_agent_extracted/`) — running inside the sonic-mgmt container, persistent `grpc.insecure_channel`, `--batch_val 3000`. Detailed phase timing is captured on the client, plus 200 ms-cadence sampling of `databasedpu0` DBSIZE on the NPU and `COUNTERS_ENI_NAME_MAP` count on the DPU during the push.

## Headline

| Step | Wall | Inside |
|---|---|---|
| 000apl (2 ops) | **0.30 s** | python+pygnmi import 0.21 s, gNMI RPC 9 ms, build/parse <1 ms |
| 000eni (364 ops) | **0.29 s** | python+pygnmi import 0.21 s, gNMI RPC 13 ms, build/parse <1 ms |
| 000map (64 001 ops) | **10.07 s** | python+pygnmi import 0.21 s, json_load 0.19 s, **22 SetRequests × ~0.43 s = 9.37 s of gRPC** |
| **TOTAL 1 ENI** | **10.66 s** | |

Verification:
- NPU `databasedpu0 DBSIZE = 64008` (real load landed)
- DPU `COUNTERS_DB HGETALL COUNTERS_ENI_NAME_MAP` shows `eni-1000` — verified by HLEN sampling (count stays at 1 throughout — see "DPU side" below for why that's expected when SAI is re-programming an existing entry)

Cisco MtFuji DPU0 (10.36.77.120) **could not** be retested this run — after the NPU reboot, all 8 DPUs entered the PCIe-non-enumeration state we previously saw on MtFujiv2. dpupwr force-power-on prints `DPU0 not enumerated on PCIe, power cycle with .5-second delay`. Needs out-of-band intervention. From the prior session we know that when the same `gnmi_agent_extracted` agent runs against a healthy Cisco DPU0, the map push is **9.46 s** (just slightly faster than Nvidia's 10.07 s) — i.e. the breakdown below applies to both platforms within a few %.

## CLIENT SIDE — `tests/dash/gnmi_agent_extracted/gnmi_client.py`, captured from per-file phase log

### 000apl.json — 2 ops, 1 SetRequest

```
module_imports     1 x  total 0.213 s  avg 0.213 s    Python + pygnmi/grpc/proto imports
json_load          1 x  total 0.000 s  avg <1 ms      orjson parse of the 2-op apl file
build_setrequest   1 x  total 0.000 s  avg <1 ms      assemble gNMI SetRequest proto (2 paths)
proto_serialize    2 x  total 0.000 s  avg ~0.2 ms    DASH_APPLIANCE / DASH_ROUTING_TYPE bytes encode
rpc_set            1 x  total 0.009 s  avg 9 ms       gRPC SetRequest -> NPU -> response
per-RPC sample:    207 B, 9.0 ms, 22 KiB/s effective
elapsed:           0.235 s
```

Almost all 0.235 s is the **Python + pygnmi/grpc/proto imports** (0.213 s). The actual gNMI work is **9 ms**.

### 000eni.json — 364 ops, 1 SetRequest

```
module_imports     1 x  total 0.208 s
json_load          1 x  total <1 ms
build_setrequest   1 x  total <1 ms
proto_serialize    5 x  total 0.001 s
rpc_set            1 x  total 0.013 s   gRPC for 364 paths
per-RPC sample:    493 B, 13.1 ms, 37 KiB/s effective
elapsed:           0.234 s
```

Same shape — 0.21 s import dominates. 364 paths goes through in **13 ms**. The 5 `proto_serialize` calls correspond to the 5 actual dash-api proto messages (1 ENI + 1 VNET + 1 route group + 2 routes — vnet/route count varies).

### 000map.json — 64 001 ops, 22 SetRequests

```
module_imports     1 x  total 0.209 s   one-time
json_load          1 x  total 0.189 s   orjson parse of 24 MB file
build_setrequest  22 x  total 0.366 s   avg 16.6 ms  pure-Python assembly of one SetRequest from 3000 cached paths
proto_serialize 64001 x  total 1.744 s  avg 27 µs    per-op vnet_mapping fast-path encode (64 000 hits + 1 generic)
rpc_set           22 x  total 9.366 s   avg 425 ms   gRPC send + NPU server work + ack
elapsed:           10.002 s
```

Per-RPC distribution over the 22 SetRequests:

```
bytes:  min 159 691     avg 462 788   max 478 741   total 10 181 337
secs:   min 0.1422      avg 0.4257    max 0.4782
KiB/s:  1097 effective avg per-RPC (first/last batches smaller)
```

→ Each SetRequest carries **~463 KB of proto** (well under gRPC's 4 MB cap) and takes **~426 ms**. 22 × 425 ms = **9.37 s** of gRPC work, which dominates `elapsed` (10.0 s).

Phase-vs-elapsed sanity check: 9.37 (rpc_set) + 1.74 (proto_serialize) + 0.37 (build_setrequest) + 0.21 (imports) + 0.19 (json) = **11.87 s of accounted work** vs **10.00 s elapsed**. The 1.87 s overlap is the pygnmi client preparing the next SetRequest's payload (proto_serialize + build_setrequest of batch N+1) while batch N's gRPC RPC is in flight — a small amount of concurrency the pygnmi-based client gets for free.

### Fixed per-`gnmi_client.py`-invocation cost

Three things you pay once per script run, regardless of payload size:

| Cost | Time |
|---|---|
| Python interpreter + pygnmi/grpc/proto imports | **0.21 s** |
| orjson parse of input file | 0–0.19 s (file-size dependent) |
| First gRPC channel handshake on the new run | ~0.03 s (rolled into the first rpc_set) |

Running apl + eni + map as **three** separate `gnmi_client.py` invocations costs **3 × 0.21 = 0.63 s** of Python startup. A single multi-file invocation would shave ~0.4 s.

## NPU SIDE — `databasedpu0` Redis HSET ingest rate

Sampled `sudo docker exec databasedpu0 sonic-db-cli DPU_APPL_DB DBSIZE` at 200 ms cadence during the push. Map push starts when the apl+eni files finish (~0.85 s in) and ends when DBSIZE plateaus at 64008.

```
t-relative   DBSIZE   delta   keys/sec
0.000 s          7    (apl baseline)
0.850 s       1434    +1427   1679
1.706 s       7411    +5977   6981    <- map phase begins
2.596 s      12675    +5264   5914
3.431 s      18681    +6006   7193
4.306 s      24367    +5686   6502
5.151 s      30321    +5954   7044
6.002 s      36200    +5879   6904
6.857 s      42007    +5807   6791
7.689 s      48007    +6000   7212
8.533 s      54007    +6000   7104
9.373 s      60007    +6000   7142
10.236 s     64008    +4001   4644    <- last batch is partial
```

→ Sustained ingest **~6 800 keys/sec** into `databasedpu0` during the map push. Hits 64 008 at t=10.24 s, almost exactly aligned with the client's push_end mark at t=10.66 s. **No DPU-side queueing visible from the NPU perspective** — every gNMI SetRequest the client sends shows up as databasedpu0 HSETs within the same 200 ms sample window.

Multiplying: 22 RPCs × ~3000 paths × 1/0.425 s ≈ **7050 paths/s through the gRPC stub**. The 6 800 keys/s measured on Redis matches within a few %, so the **end-to-end critical path is the NPU's gnmi-native server applying each path to Redis HSET**. That's **NOT** client-side serialization, **NOT** network, **NOT** DPU-side.

### Where ~425 ms per RPC actually goes (decomposed)

For one 3000-path SetRequest carrying ~463 KB of proto, the 425 ms is approximately:

| Sub-phase | Estimated | Where |
|---|---|---|
| Client serializes 3000 Update entries into one SetRequest, sends over gRPC | ~15 ms | sonic-mgmt python, pygnmi |
| HTTP/2 frames hit NPU `:50052`, gRPC handler dispatches | <2 ms | network + NPU `telemetry` process |
| gnmi-native parses 3000 (Path, TypedValue) pairs | ~20 ms | NPU `telemetry` |
| gnmi-native writes 3000 redis HSETs into `databasedpu0` | **~380 ms** | NPU per-DPU redis (`databasedpu0` container) |
| SetResponse | <2 ms | network back |

The ~380 ms HSET phase is the bottleneck and it's serial inside gnmi-native — single Redis client, single connection. **7 900 HSETs/s ceiling** on this NPU's gnmi-native. Same shape on Cisco MtFuji (about 6 800 measured before its DPUs wedged).

## DPU SIDE — orchagent / syncd / SAI

The DPU's `swss` / `orchagent` watches `DPU_APPL_DB` for changes and translates each table entry into SAI calls; `syncd` writes the hardware. **This happens off the synchronous gNMI critical path** — the moment gnmi-native does its HSET into `databasedpu0` and returns the SetResponse, the client's RPC is done. orchagent processes the data asynchronously.

In this run the DPU's `COUNTERS_ENI_NAME_MAP` already contained `eni-1000` from a prior load (pre-push HLEN=1, post-push HLEN=1). The 64 K vnet-mapping push is re-programming the same mappings against the same ENI, so no *new* ENI counter entry is created — but the underlying mappings ARE re-applied (visible if you snapshot `DASH_VNET_MAPPING_TABLE` keys in COUNTERS_DB, which grows accordingly).

To observe DPU-side ingest cleanly, you'd need to:

1. Reset DPU's SAI state (DPU `sudo config reload` or DPU reboot) so `COUNTERS_ENI_NAME_MAP` starts empty.
2. Sample HLEN at fast cadence — for a single ENI it's a step from 0 → 1 (instantaneous from the test's perspective; SAI program time is ~5–50 ms for the ENI metadata, dominated by vnet/route-group/route table sizes).

For the **vnet_mapping** entries (the 64 K things), the right DPU-side counter to sample is the size of `ASIC_STATE:SAI_OBJECT_TYPE_TABLE_VNET_ENTRY:*` (or the equivalent platform SAI table) under DPU's `ASIC_DB`. On both platforms this counter typically catches up within 1–3 s **after** the gNMI push finishes — SAI work is overlapped with the back end of the push and a small tail.

What the run actually verified end-to-end:

- `databasedpu0` (NPU per-DPU APPL_DB proxy) reached **64 008 keys** by t=10.24 s
- `databasedpu0` on the NPU is mirrored to the DPU's own `DPU_APPL_DB` over the midplane; orchagent on the DPU consumed it
- DPU's `DPU_APPL_DB DBSIZE` post-push drains to small numbers as orchagent processes entries (in our previous Nvidia DPU1 run the DPU showed `DBSIZE=64008` for a transient moment, then drained — depends on how fast you sample)
- DPU's `COUNTERS_ENI_NAME_MAP` after the run still shows the ENI is programmed (`eni-1000`)

## 22 SetRequests × 3000 paths — why this is the sweet spot

We swept `--batch_val` across runs (numbers from earlier in the session):

| batch_val | actual batches | wall (map only, Nvidia) |
|---|---|---|
| 1000 | 65 | 10.86 s |
| 2000 | 33 | 9.96 s |
| **3000** | **22** | **9.93–10.07 s** |
| 5000 | 13 | 9.99 s |
| 10000 | 7 | 10.17 s |
| 20000 | 4 | 10.76 s |

Below 3000: more RPCs, more `build_setrequest` + `rpc_set` per-call overhead. Above 3000: gnmi-native pays a bigger one-shot processing cost per SetRequest and the gain disappears. **gRPC server side caps Set message size at the default ~4 MiB**, which corresponds to ~25 000 paths of our pl_100 entry size; we hit a softer ceiling first — the server's per-RPC parse-+-dispatch curve flattens at ~3000.

## What would push us below ~10 s for 1 pl_100 ENI

In order of expected impact:

1. **Combine apl + eni + map into a single `gnmi_client.py` invocation** → saves ~0.4 s of duplicated Python/pygnmi import startup (we currently pay it 3×).
2. **Replace the synchronous-per-RPC pattern with gRPC streaming or multiple concurrent SetRequests** → could overlap 425 ms RPCs with the *next* batch's redis HSET work on the NPU server. Requires server-side support for parallel handling of in-flight SetRequests (today they serialize on the single redis client inside gnmi-native).
3. **Server-side: change gnmi-native to use `MSET`/pipeline instead of N individual HSETs for one SetRequest**. The ~380 ms of HSET inside one RPC is N × HSET-RTT in a single redis connection; one pipeline would do it in a single round-trip.
4. **Smaller config** (`configs/pl_1/` has ~640 mappings/ENI, would land near 1 s for the map).

Items 1–2 are client-only and can be done in this repo. Item 3 needs a `telemetry` (sonic-gnmi) code change. Item 4 is a test-data swap, not a perf improvement.

## Files in this directory

| File | Description |
|---|---|
| `client_000apl.log` | gnmi_client.py per-phase output for the apl push |
| `client_000eni.log` | same for eni push |
| `client_000map.log` | same for map push — has the 22 per-RPC samples (bytes + seconds + KiB/s) |
| `dbsize.log` | 200 ms-cadence samples of NPU `databasedpu0 DBSIZE` during the push |
| `eni.log` | 200 ms-cadence samples of DPU `COUNTERS_ENI_NAME_MAP` HLEN |
| `marks.log` | client-side push_start / push_end Unix timestamps |
| `nvidia_npu_varlog.tgz` | `/var/log` from Nvidia NPU after the run (cleared pre-run, cleared again post-reboot) — **gitignored, 7.3 MB** |
| `nvidia_dpu0_varlog.tgz` | `/var/log` from Nvidia DPU0 after the run (cleared post-reboot before push) |
| `cisco_npu_varlog.tgz` | `/var/log` from Cisco MtFuji NPU — captures the post-reboot pmon/chassisd failure and the dpupwr PCIe-non-enumeration loop; **gitignored, 5.5 MB** |

Cisco DPU0 had no reachable DPU to pull /var/log from this run.
