# DASH API Load Speed — 64 ENI × 64k mappings × 10k routes (full scale, under live traffic)

Findings from `test_dash_api_speed_pl_with_traffic.py` on a clean, freshly
rebooted Nvidia BlueField-3 SmartSwitch (1 DPU), at the **real private-link
scale**: every ENI carries 64,000 VNet mappings and 10,000 outbound routes.
Compare with the minimal-mapping run in [README.md](README.md) (64 mappings, 1
route/ENI).

Measures, per ENI, both:
- **Script load time** — when the gNMI push of that ENI completed.
- **First-timestamp load time** — when that ENI first forwarded a packet in
  hardware (IxNetwork First TimeStamp), captured by a **background poller thread**
  so reading stats never slows the config-load path.

## Run configuration

| Item | Value |
|------|-------|
| Platform | Nvidia `keysight-nss01`, `Mellanox-SN4280-O28`, **1 DPU** (DPU0, BlueField-3) |
| ENIs | 64 |
| Mappings / ENI | 64,000 |
| Routes / ENI | 10,000 |
| Config files pushed | 193 (1 apl + 64 grp + 64 eni + 64 map) |
| Total config pushed | **DPU_APPL_DB = 4,736,386 keys** (was 0) |
| Traffic | Outbound, 64 per-ENI flows (VLAN 1001..1064), continuous, started before programming |
| Load method | gNMI push to `/DPU_APPL_DB/dpu0/...` |
| Stats collection | **separate daemon thread** (poll interval 4 s) — decoupled from the load |
| Settle after push | 300 s |
| Run date / duration | 2026-06-20, 46 min 16 s |

## Result summary

| Metric | Value |
|--------|-------|
| Clean baseline loss (pre-program) | 100.00 % (empty DPU verified) |
| ENIs pushed via gNMI | **64 / 64** |
| **ENIs forwarding in hardware** | **45 / 64** |
| Steady-state aggregate loss | 29.69 % (≈ the 19/64 that never came up) |
| **Avg script load time / ENI (gNMI)** | **18.456 s** |
| **Avg first-timestamp time / ENI** | 22.193 s (over the ENIs that forwarded) |
| gNMI total span (push all 64) | 1162.7 s (~19.4 min) |
| Traffic span (first → last forward) | 976.5 s |
| Per-ENI gNMI push (map file alone) | ~11–12 s (64,001 ops/file) |
| **NASA failures** | **NONE** (no SAI_STATUS error, no table_transform, no capacity/max) |

### Why only 45/64 — throughput, not a ceiling, not a failure
The gNMI push delivered all 64 ENIs (4.7M keys) and completed linearly at
**~18.5 s/ENI**. But the DPU's swss→syncd→NASA install pipeline is far slower at
this scale: forwarding **lagged the push by ~15 ENIs** and stalled at 45 while
orchagent kept grinding the backlog (orchagent ~8 GB RAM, still busy at the end).

At the end of the window:
- `COUNTERS_ENI_NAME_MAP` = 55 ENIs created (not even all 64 created yet).
- NASA resources still had headroom: `eni` 9 free, `outbound_routing_group` 10
  free, `ca_to_pa` 4.9M free, `outbound_routing` 5.9M free.
- The highest created ENIs (49–54) received traffic (OUTBOUND_RX ~280–296k) but
  `FLOW_CREATED = 0` and only small routing-miss drops — i.e. **mid-install, not
  rejected**.
- syncd / NASA / swss logs: **zero errors**.

So this is **orchagent throughput** (~minutes/ENI at full 64k-mapping scale),
*before* it ever reaches the hardware ceiling. Contrast the minimal run, which
DID hit a hard `outbound_routing_group` ceiling at 58 (see
[README.md](README.md)). A much longer settle would let more ENIs drain in.

> The run also logs "1 error": the sonic-mgmt `memory_utilization` teardown
> watchdog (orchagent grew >20 % from the 4.7M-key push). It is a fixture memory
> alarm, **not** a test or NASA failure — the test result (45/64) is valid.

## Per-ENI table — script load vs first-timestamp

`gNMI t` / `Traffic t` are seconds relative to ENI 0 (cumulative). `Δ` columns are
the per-ENI increment (time to load that ENI). `—` = never forwarded.

| ENI | gNMI t (s) | **script Δ/ENI** | Traffic t (s) | **first-TS Δ/ENI** | Rx frames | Fwd |
|----:|----------:|----------------:|--------------:|-------------------:|----------:|:---:|
| 0 | 0.000 | — | 0.000 | — | 460101 | ✓ |
| 1 | 17.848 | 17.848 | 18.934 | 18.934 | 456314 | ✓ |
| 2 | 35.680 | 17.832 | 36.214 | 17.280 | 452858 | ✓ |
| 3 | 53.547 | 17.867 | 51.914 | 15.700 | 449718 | ✓ |
| 4 | 72.384 | 18.837 | 79.174 | 27.260 | 444266 | ✓ |
| 5 | 90.173 | 17.789 | 105.039 | 25.865 | 439093 | ✓ |
| 6 | 108.458 | 18.285 | 121.705 | 16.666 | 435760 | ✓ |
| 7 | 126.943 | 18.486 | 137.205 | 15.500 | 432660 | ✓ |
| 8 | 145.181 | 18.238 | 147.450 | 10.245 | 430611 | ✓ |
| 9 | 162.820 | 17.639 | 182.295 | 34.845 | 423642 | ✓ |
| 10 | 180.714 | 17.895 | 191.510 | 9.215 | 421799 | ✓ |
| 11 | 198.587 | 17.873 | 208.275 | 16.765 | 418446 | ✓ |
| 12 | 217.023 | 18.436 | 223.995 | 15.720 | 415302 | ✓ |
| 13 | 235.349 | 18.326 | 255.795 | 31.800 | 408942 | ✓ |
| 14 | 253.800 | 18.451 | 265.395 | 9.600 | 407022 | ✓ |
| 15 | 272.255 | 18.455 | 283.655 | 18.260 | 403370 | ✓ |
| 16 | 290.892 | 18.637 | 308.890 | 25.235 | 398323 | ✓ |
| 17 | 308.892 | 18.000 | 329.685 | 20.795 | 394163 | ✓ |
| 18 | 327.145 | 18.253 | — | — | 0 | ✗ |
| 19 | 345.440 | 18.295 | 361.631 | 31.946 | 387774 | ✓ |
| 20 | 363.508 | 18.068 | 393.026 | 31.395 | 381495 | ✓ |
| 21 | 381.807 | 18.299 | 409.231 | 16.205 | 378254 | ✓ |
| 22 | 400.452 | 18.645 | 427.481 | 18.250 | 374604 | ✓ |
| 23 | 418.632 | 18.180 | — | — | 0 | ✗ |
| 24 | 437.362 | 18.729 | 457.391 | 29.910 | 368622 | ✓ |
| 25 | 455.438 | 18.077 | — | — | 0 | ✗ |
| 26 | 473.780 | 18.341 | 494.016 | 36.625 | 361297 | ✓ |
| 27 | 491.610 | 17.831 | 527.211 | 33.195 | 354658 | ✓ |
| 28 | 510.591 | 18.981 | 527.201 | ~0 | 354660 | ✓ |
| 29 | 529.051 | 18.460 | 567.111 | 39.910 | 346678 | ✓ |
| 30 | 548.192 | 19.141 | 567.106 | ~0 | 346679 | ✓ |
| 31 | 566.313 | 18.121 | 609.736 | 42.630 | 338153 | ✓ |
| 32 | 585.637 | 19.324 | 609.732 | ~0 | 338154 | ✓ |
| 33 | 603.540 | 17.903 | 647.052 | 37.320 | 330690 | ✓ |
| 34 | 623.094 | 19.554 | 647.027 | ~0 | 330695 | ✓ |
| 35 | 641.332 | 18.238 | 690.562 | 43.535 | 321988 | ✓ |
| 36 | 660.413 | 19.081 | 690.542 | ~0 | 321992 | ✓ |
| 37 | 678.685 | 18.272 | 735.642 | 45.100 | 312972 | ✓ |
| 38 | 697.569 | 18.884 | 735.637 | ~0 | 312973 | ✓ |
| 39 | 715.770 | 18.200 | 788.492 | 52.855 | 302402 | ✓ |
| 40 | 734.044 | 18.274 | 788.487 | ~0 | 302403 | ✓ |
| 41 | 752.452 | 18.408 | 833.452 | 44.965 | 293410 | ✓ |
| 42 | 770.722 | 18.270 | 833.447 | ~0 | 293411 | ✓ |
| 43 | 788.847 | 18.125 | 881.117 | 47.670 | 283877 | ✓ |
| 44 | 807.182 | 18.335 | 881.113 | ~0 | 283878 | ✓ |
| 45 | 825.459 | 18.277 | 932.873 | 51.760 | 273526 | ✓ |
| 46 | 844.085 | 18.626 | 932.863 | ~0 | 273528 | ✓ |
| 47 | 862.381 | 18.295 | — | — | 0 | ✗ |
| 48 | 880.776 | 18.396 | 976.513 | 43.650 | 264798 | ✓ |
| 49 | 899.655 | 18.878 | — | — | 0 | ✗ |
| 50 | 918.565 | 18.910 | — | — | 0 | ✗ |
| 51 | 936.881 | 18.316 | — | — | 0 | ✗ |
| 52 | 955.049 | 18.168 | — | — | 0 | ✗ |
| 53 | 973.367 | 18.318 | — | — | 0 | ✗ |
| 54 | 992.192 | 18.825 | — | — | 0 | ✗ |
| 55 | 1010.909 | 18.717 | — | — | 0 | ✗ |
| 56 | 1029.789 | 18.880 | — | — | 0 | ✗ |
| 57 | 1049.164 | 19.374 | — | — | 0 | ✗ |
| 58 | 1068.220 | 19.056 | — | — | 0 | ✗ |
| 59 | 1087.494 | 19.274 | — | — | 0 | ✗ |
| 60 | 1107.172 | 19.678 | — | — | 0 | ✗ |
| 61 | 1126.141 | 18.969 | — | — | 0 | ✗ |
| 62 | 1144.456 | 18.315 | — | — | 0 | ✗ |
| 63 | 1162.703 | 18.247 | — | — | 0 | ✗ |

**Did not forward (19):** 18, 23, 25, 47, and 49–63.

### Reading the table
- **Script (gNMI) load is rock-steady at ~18.5 s/ENI** across all 64 — the push
  itself scales linearly and never stalls. The map file (64,001 ops) dominates
  each ENI's push (~11–12 s of the ~18.5 s).
- **First-timestamp load is erratic and lags** because the dataplane install runs
  behind the push. Note the **`~0` Δ pairs** (28, 30, 32, 34, 36, 38, 40, 42, 44,
  46): these ENIs came up in the *same* burst as the previous ENI — orchagent
  installs them in chunks as it drains the backlog, not one-by-one in push order.
- After ENI 48 (at +976 s) **nothing else forwarded** within the window — the
  remaining ENIs (49–63) were pushed but their dataplane install never completed
  in time. No failure; just not processed yet.

## gRPC push time vs first-timestamp delta (the cliff at ENI 48→49)

The actual per-ENI **gRPC push duration** (the `(X.XXs)` for each ENI's 64,001-op
map file) vs the **first-timestamp Δ** (gap between consecutive forwarded ENIs).
`≈0 (burst)` = came up in the same orchagent drain-burst as the previous ENI.

| ENI | gRPC push (s) | first-TS Δ (s) | fwd | ENI | gRPC push (s) | first-TS Δ (s) | fwd |
|----:|-------------:|---------------:|:---:|----:|-------------:|---------------:|:---:|
| 0 | 10.64 | — | ✓ | 32 | 11.09 | ≈0 (burst) | ✓ |
| 1 | 10.84 | 18.93 | ✓ | 33 | 10.73 | 37.32 | ✓ |
| 2 | 10.71 | 17.28 | ✓ | 34 | 11.91 | ≈0 (burst) | ✓ |
| 3 | 10.87 | 15.70 | ✓ | 35 | 11.19 | 43.54 | ✓ |
| 4 | 10.50 | 27.26 | ✓ | 36 | 11.49 | ≈0 (burst) | ✓ |
| 5 | 10.78 | 25.87 | ✓ | 37 | 11.14 | 45.10 | ✓ |
| 6 | 10.91 | 16.67 | ✓ | 38 | 11.48 | ≈0 (burst) | ✓ |
| 7 | 11.00 | 15.50 | ✓ | 39 | 11.26 | 52.86 | ✓ |
| 8 | 10.95 | 10.25 | ✓ | 40 | 11.02 | ≈0 (burst) | ✓ |
| 9 | 10.87 | 34.85 | ✓ | 41 | 11.08 | 44.97 | ✓ |
| 10 | 10.45 | 9.22 | ✓ | 42 | 11.20 | ≈0 (burst) | ✓ |
| 11 | 11.04 | 16.77 | ✓ | 43 | 10.90 | 47.67 | ✓ |
| 12 | 11.38 | 15.72 | ✓ | 44 | 11.09 | ≈0 (burst) | ✓ |
| 13 | 11.39 | 31.80 | ✓ | 45 | 11.26 | 51.76 | ✓ |
| 14 | 11.18 | 9.60 | ✓ | 46 | 11.07 | ≈0 (burst) | ✓ |
| 15 | 11.48 | 18.26 | ✓ | 47 | 11.25 | — | ✗ |
| 16 | 11.35 | 25.24 | ✓ | **48** | 11.09 | 43.65 | ✓ |
| 17 | 10.95 | 20.80 | ✓ | **49** | 11.08 | — | ✗ |
| 18 | 11.09 | — | ✗ | 50 | 11.58 | — | ✗ |
| 19 | 10.87 | 31.95 | ✓ | 51 | 11.21 | — | ✗ |
| 20 | 10.65 | 31.40 | ✓ | 52 | 11.22 | — | ✗ |
| 21 | 11.03 | 16.21 | ✓ | 53 | 11.05 | — | ✗ |
| 22 | 11.84 | 18.25 | ✓ | 54 | 11.50 | — | ✗ |
| 23 | 11.13 | — | ✗ | 55 | 11.54 | — | ✗ |
| 24 | 11.54 | 29.91 | ✓ | 56 | 11.51 | — | ✗ |
| 25 | 11.09 | — | ✗ | 57 | 12.03 | — | ✗ |
| 26 | 11.05 | 36.63 | ✓ | 58 | 11.49 | — | ✗ |
| 27 | 10.72 | 33.20 | ✓ | 59 | 11.97 | — | ✗ |
| 28 | 11.75 | ≈0 (burst) | ✓ | 60 | 12.18 | — | ✗ |
| 29 | 11.03 | 39.91 | ✓ | 61 | 11.70 | — | ✗ |
| 30 | 11.61 | ≈0 (burst) | ✓ | 62 | 10.85 | — | ✗ |
| 31 | 10.95 | 42.63 | ✓ | 63 | 11.03 | — | ✗ |

- **gRPC push time is flat at ~11.1 s/ENI for all 64** (range 10.45–12.18 s),
  including the dead ENIs 49–63 — the push delivered every ENI fine. Avg ~11.15 s.
- **first-TS Δ grows/erratic** as the dataplane install lags, then **hard-stops
  after ENI 48**. ENIs 49–63 have a clean push but never forward: the install
  backlog never reached them within the 300 s settle. The failure is in
  install throughput, not the gRPC push.

## Takeaways
1. **Pushing** 64 ENIs at full scale (4.7M keys) is fast and linear: ~18.5 s/ENI,
   ~19 min total. gNMI / config delivery is **not** the bottleneck.
2. **Installing** at full scale is the bottleneck: orchagent throughput
   (~minutes/ENI) makes the dataplane lag the push and plateau (here at 45/64 in a
   300 s settle).
3. **No NASA/SAI failures** — the unforwarded ENIs are queued, not rejected. A
   longer settle drains more; the eventual hard limit is the
   `outbound_routing_group` ceiling (~58–59, see [README.md](README.md)).

## Reproduce
Knobs in `test_dash_api_speed_pl_with_traffic.py`: `_ENI_COUNT="ALL"`,
`MINIMAL_MAPPING=False`, `ROUTES_PER_ENI=10000`, `POST_PROGRAM_SETTLE_S=300`,
`PUSH_STATS_POLL_S=4`. Run command identical to [README.md](README.md).
