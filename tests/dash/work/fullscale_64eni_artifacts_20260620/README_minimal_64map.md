# DASH API Load Speed — 64 ENI × 64 mappings × 1 route (under live traffic)

Findings from `test_dash_api_speed_pl_with_traffic.py` run on a clean, freshly
rebooted Nvidia BlueField-3 SmartSwitch. Measures **how long it takes to load
an ENI in hardware** when programmed via gNMI while live outbound traffic runs,
and correlates each ENI's gNMI push with the moment it first forwards a packet.

## Run configuration

| Item | Value |
|------|-------|
| Platform | Nvidia SmartSwitch `keysight-nss01`, hwsku `Mellanox-SN4280-O28` |
| DPU | DPU0 (BlueField-3), midplane `169.254.200.1`, dataplane `10.0.0.57` |
| ENIs | 64 (single DPU) |
| Mappings / ENI | 64 (VNET mappings) |
| Routes / ENI | 1 |
| Config files pushed | 193 (ACL group + ACL rules + ENI/route + map, per ENI) |
| Traffic | Outbound only (VXLAN in → NVGRE out), 64 per-ENI flows, one VLAN per ENI (1001..1064), **continuous, started before programming** |
| Load method | gNMI push to `/DPU_APPL_DB/dpu0/...` |
| Run date | 2026-06-20 (DUT clock), 15 min 17 s total |

### Methodology (why the numbers are trustworthy)
1. **Reboot the NPU first** so the DPU starts with an empty DASH config
   (`DPU_APPL_DB DBSIZE = 0`). Persist correct NPU static routes in
   `config_db.json` before the reboot: `221.1.0.0/16 → Ethernet0` (outbound),
   `221.2.0.0/16 → Ethernet8` (return).
2. **Start traffic before programming** and take a baseline window — it must show
   ~100 % loss. This run: **baseline aggregate loss 100.00 %, 0 flows** → proves
   nothing forwards until we program it. (If baseline forwards, the DPU still has
   stale config and every "first timestamp" is meaningless.)
3. Clear IxNetwork stats, then push all 64 ENIs while traffic runs. Each flow's
   **First TimeStamp** = the instant that ENI first forwarded a packet in HW.

## Result summary

| Metric | Value |
|--------|-------|
| ENIs programmed (gNMI) | 64 / 64 |
| **ENIs forwarding in hardware** | **58 / 64** |
| Steady-state aggregate loss | 9.37 % (= exactly the 6 ENIs that never came up; the 58 are lossless) |
| **Avg time to load one ENI** | **6.274 s / ENI** (traffic-first-seen cadence) |
| Avg gNMI push cadence | 6.267 s / ENI |
| Traffic-vs-push offset | ~0.1 s — each ENI forwards within ~100 ms of its push completing |
| Total bring-up span (ENI 0 → 57) | 357.639 s |
| Per-ENI Δ range | 5.565 s (ENI 38) … 7.280 s (ENI 37) |

**Key takeaway on load time:** per-ENI hardware bring-up is **push-bound**, not
install-latency-bound. The ASIC installs each ENI within ~100 ms of receiving it;
the 6.27 s/ENI cadence is the rate at which the gNMI flow serializes each ENI's
~3 config files (≈2 s/file). To load ENIs faster, speed up / batch the push — the
hardware is not the bottleneck for the first 58.

## Per-ENI first-forwarding timestamp & inter-ENI delta

Times are seconds relative to ENI 0's first forwarded frame.

| ENI | First TS (s) | Δ prev (s) | ENI | First TS (s) | Δ prev (s) |
|----:|------------:|-----------:|----:|------------:|-----------:|
| 0  | 0.000   | —     | 32 | 201.017 | 6.230 |
| 1  | 6.320   | 6.320 | 33 | 207.227 | 6.210 |
| 2  | 12.625  | 6.305 | 34 | 213.448 | 6.221 |
| 3  | 18.880  | 6.255 | 35 | 219.688 | 6.240 |
| 4  | 25.105  | 6.225 | 36 | 225.928 | 6.240 |
| 5  | 31.355  | 6.250 | 37 | 233.208 | 7.280 |
| 6  | 37.620  | 6.265 | 38 | 238.773 | 5.565 |
| 7  | 43.870  | 6.250 | 39 | 245.013 | 6.240 |
| 8  | 50.100  | 6.230 | 40 | 251.238 | 6.225 |
| 9  | 56.606  | 6.506 | 41 | 257.473 | 6.235 |
| 10 | 62.961  | 6.355 | 42 | 263.683 | 6.210 |
| 11 | 69.191  | 6.230 | 43 | 270.028 | 6.345 |
| 12 | 75.431  | 6.240 | 44 | 276.173 | 6.145 |
| 13 | 81.671  | 6.240 | 45 | 282.408 | 6.235 |
| 14 | 87.991  | 6.320 | 46 | 288.648 | 6.240 |
| 15 | 94.251  | 6.260 | 47 | 295.049 | 6.401 |
| 16 | 100.466 | 6.215 | 48 | 301.274 | 6.225 |
| 17 | 106.706 | 6.240 | 49 | 307.509 | 6.235 |
| 18 | 113.446 | 6.740 | 50 | 313.769 | 6.260 |
| 19 | 119.526 | 6.080 | 51 | 320.064 | 6.295 |
| 20 | 125.791 | 6.265 | 52 | 326.304 | 6.240 |
| 21 | 132.051 | 6.260 | 53 | 332.549 | 6.245 |
| 22 | 138.317 | 6.266 | 54 | 338.764 | 6.215 |
| 23 | 144.567 | 6.250 | 55 | 344.994 | 6.230 |
| 24 | 150.787 | 6.220 | 56 | 351.239 | 6.245 |
| 25 | 157.017 | 6.230 | 57 | 357.639 | 6.400 |
| 26 | 163.267 | 6.250 | **58** | — | never forwarded |
| 27 | 169.477 | 6.210 | **59** | — | never forwarded |
| 28 | 175.942 | 6.465 | **60** | — | never forwarded |
| 29 | 182.232 | 6.290 | **61** | — | never forwarded |
| 30 | 188.522 | 6.290 | **62** | — | never forwarded |
| 31 | 194.787 | 6.265 | **63** | — | never forwarded |

## Why the last 6 ENIs (58–63) never loaded

**Root cause: the DPU's `outbound_routing_group` hardware/NASA resource pool is
exhausted (~59 per DPU).** It is *not* a config bug, push ordering, settle time,
ENI count, route count, or mapping count.

In this private-link config **each ENI gets its own outbound routing group**, so
64 ENIs need 64 groups. The BlueField-3 pool caps around 58–59. The first 58
ENIs each obtained a routing group (→ their route installed → they forward);
ENIs 58–63 were created and *receive* traffic but could not obtain a routing
group, so they have **no outbound route** and every packet is dropped.

This fails **silently** — no SAI error is logged in `syncd`/`swss`; NASA simply
reports the resource as unavailable.

### Evidence

Per-ENI hardware counters (`redis-cli -n 2 HGETALL COUNTERS:<eni_oid>` on the DPU):

| ENI | OUTBOUND_RX | FLOW_CREATED | ROUTING_ENTRY_MISS_DROP | CA_PA_MISS |
|-----|-----------:|:------------:|------------------------:|:----------:|
| 56 (ok)   | 41 225 | 1 | 914 (only pre-bring-up pkts) | 0 |
| 57 (ok)   | 39 941 | 1 | 906 | 0 |
| 58 (fail) | 38 689 | 0 | 77 374 (100 % of RX) | 0 |
| 59–63 (fail) | ~33–37 k | 0 | ~65–75 k (100 % of RX) | 0 |

- Failing ENIs **do** receive traffic (`OUTBOUND_RX > 0`) → VXLAN reaches the DPU,
  VIP matches, ENI/MAC lookup succeeds, ENI object exists.
- `CA_PA_ENTRY_MISS = 0` → mappings are fine.
- `FLOW_CREATED = 0` and **100 % of received packets hit
  `OUTBOUND_ROUTING_ENTRY_MISS_DROP`** → the outbound *route* never installed.

NASA resource availability (`nasa_cli.py resource_availability_get`, values are
*remaining*, not max):

| Resource | Free | Verdict |
|----------|-----:|---------|
| `eni` | 0 | at ceiling (64 max, all used — all 64 ENIs exist) |
| `eni_ether_addr_map` | 0 | 64 used (ENI lookup works for all 64) |
| **`outbound_routing_group`** | **1** | **binding constraint (~59 total, 58 used)** |
| `outbound_routing` (entries) | 6 399 872 | abundant — not the limit |
| `ca_to_pa` (mappings) | 7 995 904 | abundant — not the limit |
| `vnet` / `acl_rule` / `acl_group` | 960 / 639 999 / 639 | fine |

The pattern is consistent: failures are always the **last N ENIs by program
order** (the group pool fills as you go), and steady-state loss is exactly the
fraction of ENIs with no group (6/64 = 9.37 %).

### How to break past 58 ENIs
- Make the DASH config **share one outbound routing group across multiple ENIs**
  (if the private-link design allows), so group usage no longer scales 1:1 with
  ENI count, **or**
- Use a DPU firmware/image with a larger `outbound_routing_group` pool.

This is a hardware/firmware ceiling — it cannot be fixed in the test or the gNMI
push path.

## Reproduce / diagnose

Run (from inside the sonic-mgmt container on the SMD server):

```bash
cd /home/dash/sonic-mgmt/sonic-mgmt/tests && \
  ANSIBLE_LIBRARY=$PWD/../ansible/library \
  ANSIBLE_MODULE_UTILS=$PWD/../ansible/module_utils \
  pytest dash/test_dash_api_speed_pl_with_traffic.py \
    --testbed=keysight-nss01 --testbed_file=../ansible/testbed.yaml \
    --inventory=../ansible/lab --host-pattern=keysight-nss01 \
    --dpu_index=0 --dpu-pattern=keysight-nss01-dpu0 --cache-clear -v
```

Check the resource ceiling on the DPU (SSH `admin@169.254.200.1`):

```bash
for r in eni outbound_routing outbound_routing_group ca_to_pa vnet; do
  printf "resource_availability_get resource_name $r\nquit\n" | \
    docker exec -i syncd python /usr/sbin/cli/nasa_cli.py -u 2>&1 | grep Availability
done
```

Knobs in `test_dash_api_speed_pl_with_traffic.py`:
`MINIMAL_MAPPING=True`, `MAPPINGS_PER_ENI=64`, `ROUTES_PER_ENI=1`,
`POST_PROGRAM_SETTLE_S=90`, ENI count auto (64 Nvidia / 32 Cisco).
