# Why DASH Private-Link INBOUND drops on NVGRE (INBOUND_ROUTING_ENTRY_MISS) — upstream research

> **RESOLVED 2026-06-19 — NVGRE inbound forwards end-to-end; NO image change needed.**
> The miss only happens on the SLOW-PATH inbound_routing table (which this analysis is about).
> Per the PL HLD the return is handled by the **reverse FLOW** created on the outbound pass, and the
> BlueField handles NVGRE on that flow path. The real bug was in our TEST: the inbound return must be
> the EXACT 5-tuple reverse of the outbound CA flow (UDP 10000/10000, not 22222) AND outbound must be
> sent first so the reverse flow exists. After that fix (commits a46313c08 / 225c85d25) inbound = 0%
> loss, `INBOUND_ROUTING_ENTRY_MISS` delta 0, test passes — with NVGRE kept on the PA side both ways.
> The slow-path/VXLAN-only analysis below remains correct but is NOT the path PL return uses.

Research date 2026-06-19. Sources: github.com/sonic-net/DASH, github.com/opencomputeproject/SAI,
github.com/sonic-net/sonic-swss, github.com/sonic-net/sonic-dash-api (all master/main), plus a live
read of our DPU (keysight-nss01 / DPU0, asic_type nvidia-bluefield).

## Symptom
Private-link INBOUND (service→VM) return traffic sent as **NVGRE** (GRE proto 0x6558, VSID 100) drops
100% at `SAI_ENI_STAT_INBOUND_ROUTING_ENTRY_MISS_DROP`. The byte-identical packet sent as **VXLAN**
(UDP 4789, vni 100) MATCHES and forwards. Direction-lookup, ENI, trusted-VNI and PA-validation all pass.

## Root cause (confirmed at every layer)
1. **Inbound routing matches on the parsed tunnel VNI.** The P4 table keys on
   `meta.eni_id`, `meta.rx_encap.vni` (exact), `meta.rx_encap.underlay_sip` (ternary).
   `dash-pipeline/bmv2/stages/inbound_routing.p4`.
2. **The DASH parser only recognizes VXLAN on ingress.** `dash-pipeline/bmv2/dash_parser.p4` transitions to
   the tunnel-parse state solely on `UDP dst_port == 4789` (VXLAN). There is **no GRE/NVGRE parser state**.
   `dash_encapsulation_t {INVALID, VXLAN, NVGRE}` and `nvgre_t.vsid` exist (`dash_headers.p4`) but `nvgre_t`
   is only EMITTED by the deparser (outbound encap) — never EXTRACTED. So for an NVGRE packet `rx_encap.vni`
   is never populated from the VSID → the exact-`vni` key cannot match → miss.
3. **SAI has no NVGRE-inbound representation.** `sai_inbound_routing_entry_t` match =
   `{switch_id, eni_id, vni, sip, sip_mask, priority}`; action enum =
   `{TUNNEL_DECAP, TUNNEL_DECAP_PA_VALIDATE, VXLAN_DECAP, VXLAN_DECAP_PA_VALIDATE}` —
   no NVGRE action, and no `sai_dash_encapsulation_t` attribute on the inbound entry, the ENI,
   direction-lookup, or trusted-VNI. `sai_dash_encapsulation_t {VXLAN, NVGRE}` exists ONLY on **outbound**
   objects (`saiexperimentaldashoutboundcatopa.h` ATTR_DASH_ENCAPSULATION; `saiexperimentaldashtunnel.h`,
   CREATE_ONLY, default VXLAN).
4. **Orchagent already programs the encap-agnostic action.** `sonic-swss orchagent/dash/dashrouteorch.cpp`
   `addInboundRouting()`: `action = pa_validation ? TUNNEL_DECAP_PA_VALIDATE : TUNNEL_DECAP`. The only config
   lever is `pa_validation` (sonic-dash-api `proto/route_rule.proto`). Nothing selects VXLAN vs NVGRE for
   inbound. The `EncapType {VXLAN, NVGRE}` in `proto/route_type.proto` applies to **outbound** staticencap.
5. **Upstream PL/inbound tests use VXLAN.** `test/test-cases/functional/saic/test_vm_to_vm_commn_udp_inbound.py`
   builds inbound as `...udp().vxlan()` dst_port 4789; `config_inbound_setup_commands.py` programs the inbound
   entry with `TUNNEL_DECAP_PA_VALIDATE`. The PL HLD CA-to-PA example uses `SAI_DASH_ENCAPSULATION_VXLAN`.

## Live confirmation on our DPU (sairedis.rec, via NPU→DPU jump)
```
SAI_OBJECT_TYPE_INBOUND_ROUTING_ENTRY {eni_id:0x7008000000022, vni:100, sip:221.2.0.0, sip_mask:255.255.255.255}
  SAI_INBOUND_ROUTING_ENTRY_ATTR_ACTION = SAI_INBOUND_ROUTING_ENTRY_ACTION_TUNNEL_DECAP
```
asic_type nvidia-bluefield, build internal.167426684-dfd1f4ecaa. We are **already on TUNNEL_DECAP**
(not legacy VXLAN_DECAP). So the miss is NOT a SAI-action / orchagent / config-order problem.

## What setting / order makes inbound work
The required object set/order (direction-lookup for the inbound VNI → ENI → inbound_routing
{eni, vni, sip/mask} with TUNNEL_DECAP[_PA_VALIDATE] → PA-validation) is **already correct and complete**
in our config. The ONLY variable that decides match/miss is the **on-the-wire encap**:

- **Path A — VXLAN inbound (upstream-supported, works today):** send the PL return as VXLAN (UDP 4789),
  vni = the inbound entry's vni (100). Proven to match end-to-end by our VXLAN-vs-NVGRE A/B. No config or
  ordering change needed beyond the UHD encapsulating inbound as VXLAN instead of NVGRE.
- **Path B — NVGRE inbound (the hard requirement):** NOT achievable via SONiC config/SAI/order on any
  upstream or current BlueField build. The SAI action is already correct (TUNNEL_DECAP, which is meant to be
  encap-agnostic); what's missing is a **Nvidia BlueField pipeline** that, under TUNNEL_DECAP, parses GRE
  0x6558 and lifts the 24-bit VSID into the same `rx_encap.vni` used by the inbound routing lookup. This is a
  DPU dataplane change at Nvidia; there is no SAI object to even express it. Escalate to Nvidia with the
  captured pcaps (tests/dash/captures/dpu_outbound_nvgre_raw_port1A.pcap shows the DPU's own well-formed
  NVGRE VSID 100, proving the value is correct on the wire).

## Source URLs
- inbound_routing.p4: https://raw.githubusercontent.com/sonic-net/DASH/main/dash-pipeline/bmv2/stages/inbound_routing.p4
- dash_parser.p4 (VXLAN-only): https://raw.githubusercontent.com/sonic-net/DASH/main/dash-pipeline/bmv2/dash_parser.p4
- dash_headers.p4 (encap enum/nvgre_t): https://raw.githubusercontent.com/sonic-net/DASH/main/dash-pipeline/bmv2/dash_headers.p4
- SAI inbound routing: https://raw.githubusercontent.com/opencomputeproject/SAI/master/experimental/saiexperimentaldashinboundrouting.h
- SAI encap enum: https://raw.githubusercontent.com/opencomputeproject/SAI/master/experimental/saitypesextensions.h
- SAI outbound ca-to-pa / tunnel encap: saiexperimentaldashoutboundcatopa.h, saiexperimentaldashtunnel.h (same repo/path)
- orchagent: https://github.com/sonic-net/sonic-swss/blob/master/orchagent/dash/dashrouteorch.cpp
- proto: https://github.com/sonic-net/sonic-dash-api  proto/route_rule.proto, route_type.proto, appliance.proto
- PL HLD: https://github.com/sonic-net/DASH/blob/main/documentation/private-link-service/private-link-service.md
- inbound VXLAN test: https://raw.githubusercontent.com/sonic-net/DASH/main/test/test-cases/functional/saic/test_vm_to_vm_commn_udp_inbound.py
- inbound config: https://raw.githubusercontent.com/sonic-net/DASH/main/test/test-cases/functional/saic/config_inbound_setup_commands.py
