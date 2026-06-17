"""DASH API load-speed test *with live traffic* (IxNetwork / RestPy).

Same config-push as ``test_dash_api_speed_pl.py`` (it reuses
``dash_api_speed_common.load_json_via_gnmi``), but with continuous IxNetwork
traffic running across the whole push so we can measure how fast each ENI
*actually* starts forwarding in hardware — and compare that against how fast
gRPC reported the config as pushed.

Topology
--------
IxNetwork ──VLAN──> UHD ──(VXLAN/NVGRE encap)──> SmartSwitch DPU ──> loop back
──VLAN──> IxNetwork. The UHD ("connect" fabric, ``10.36.78.39``) bridges each
VLAN to the DPU's VXLAN/NVGRE. One unique VLAN per ENI: outbound flow for global
ENI index ``g`` uses VLAN ``VLAN_OUT_BASE + g`` (== the gNMI per-ENI file index),
so flow ↔ ENI maps 1:1.

Instead of loading a saved .ixncfg, this test **builds the IxNetwork config live
via RestPy** (``dash_traffic_ixn_build.build_outbound_config``) using the exact
arithmetic from ``configs/dash_api_speed_pl/render.py``.

Flow
----
1. Build the target DPU's outbound traffic (32 ENI flows, tracked by VLAN).
2. Start continuous traffic *before* programming → ~100% loss baseline.
3. Push all ENIs via gNMI while traffic runs. As each ENI lands in hardware its
   flow starts passing; the IxNetwork per-flow "First TimeStamp" records when.
4. ``Δ(first_ts[i+1]-first_ts[i])`` = hardware per-ENI bring-up time; compare vs
   ``Δ(grpc_complete[i+1]-grpc_complete[i])`` and print a table.
5. Collect IxNetwork flow stats, switch ``show interface counters`` (duthost +
   dpuhost), and UHD per-port metrics around the push.

Run (from inside the sonic-mgmt container, like the gRPC-only speed test)::

    pytest dash/test_dash_api_speed_pl_with_traffic.py \
        --testbed=keysight-nss01 --testbed_file=../ansible/testbed.yaml \
        --inventory=../ansible/lab --host-pattern=keysight-nss01 \
        --dpu_index=0 --dpu-pattern=keysight-nss01-dpu0 --cache-clear -v

Prereq: the UHD must be loaded with the matching smartswitch config so the
chassis links come up (see test_dash_api_speed_pl_with_traffic/smartswitch-nvidia.http).
"""
import fnmatch
import importlib.util
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import time

import pytest
import dash_uhd_stats
from dash_api_speed_common import (
    _collect_memory,
    _collect_redis_memory,
    _print_results,
    dpu_pre_config,
    load_json_via_gnmi,
    npu_pre_config,
    parse_file_index,
)
from dash_traffic_ixn_build import (
    build_outbound_config,
    build_inbound_config,
    VLAN_OUT_BASE,
    VLAN_IN_BASE,
    OUTBOUND_FRAME_COUNT,
    INBOUND_FRAME_COUNT,
)

try:
    from ixnetwork_restpy import SessionAssistant
except ImportError as e:  # pragma: no cover - import guard
    raise pytest.skip.Exception(
        "Test requires ixnetwork_restpy: " + repr(e), allow_module_level=True
    )

_RENDER_PATH = os.path.join(os.path.dirname(__file__), "configs", "dash_api_speed_pl", "render.py")
_render_spec = importlib.util.spec_from_file_location("dash_render", _RENDER_PATH)
render = importlib.util.module_from_spec(_render_spec)
sys.modules["dash_render"] = render
_render_spec.loader.exec_module(render)

logger = logging.getLogger(__name__)

pytestmark = [
    pytest.mark.topology("smartswitch"),
    pytest.mark.skip_check_dut_health,
    pytest.mark.disable_loganalyzer,
    pytest.mark.sanity_check(skip_sanity=True),
]

# How many ENIs to push per DPU. "ALL" = every rendered file.
# NOTE: the DPU's DASH orchagent stalls programming the full 64-ENI x 64k-mapping
# load into the ASIC (ASIC_DB stays ~empty). A small count lets the DPU actually
# program + forward, for a green end-to-end run. Raise once the DPU scales.
_ENI_COUNT = 1

# ════════════════════════════════════════════════════════════════════════════
#  IXIA / UHD CONFIG  —  EDIT FOR YOUR TESTBED
# ════════════════════════════════════════════════════════════════════════════
# IxNetwork API server (chassis/ports + per-flow arithmetic live in
# dash_traffic_ixn_build.py's CONFIG block).
IXIA_API_SERVER_IP = "10.36.78.95"
IXIA_API_SERVER_PORT = 11009
IXIA_API_USER = None
IXIA_API_PASSWORD = None

# UHD "connect" fabric — per-port encap/decap counters.
UHD_IP = "10.36.78.39"
# UHD physical port names to sample (Port 1..4 = Nvidia DPU ports).
UHD_PORT_NAMES = ["Port 1", "Port 2", "Port 3", "Port 4"]

# Baseline (pre-program) loss check: a baseline burst with no ENIs programmed
# should drop ~everything.
BASELINE_MIN_LOSS_PCT = 99.0

# Fixed-burst model: after programming, let the DPU settle, then send one fixed
# burst and measure the deltas around exactly that burst.
POST_PROGRAM_SETTLE_S = 30   # ASIC bring-up time before the measurement burst
BURST_TIMEOUT_S = 120        # max wait for a fixed burst to finish sending
BURST_STATS_SETTLE_S = 8     # let Flow Statistics catch up after a burst
SETTLE_LOSS_PCT = 1.0        # pass/fail threshold on the measurement burst
# ════════════════════════════════════════════════════════════════════════════


# ─────────────────────────── IxNetwork / RestPy helpers ────────────────────
def _ix_connect():
    """Open a RestPy session (ClearConfig — we build fresh). Returns (sa, ixnetwork)."""
    logger.info("IxNetwork: connecting to API server %s:%s",
                IXIA_API_SERVER_IP, IXIA_API_SERVER_PORT)
    kwargs = dict(
        IpAddress=IXIA_API_SERVER_IP, RestPort=IXIA_API_SERVER_PORT,
        SessionName="dash-api-speed-traffic", ClearConfig=True,
        LogLevel=SessionAssistant.LOGLEVEL_INFO,
    )
    if IXIA_API_USER:
        kwargs["UserName"] = IXIA_API_USER
    if IXIA_API_PASSWORD:
        kwargs["Password"] = IXIA_API_PASSWORD
    sa = SessionAssistant(**kwargs)
    return sa, sa.Ixnetwork


def _ix_start_traffic(ixnetwork):
    logger.info("IxNetwork: starting continuous traffic")
    ixnetwork.Traffic.StartStatelessTrafficBlocking()


def _ix_stop_traffic(ixnetwork):
    try:
        ixnetwork.Traffic.StopStatelessTrafficBlocking()
    except Exception:
        logger.exception("IxNetwork: stop traffic failed (non-fatal)")


def _ix_run_fixed_burst(ixnetwork, label, timeout=BURST_TIMEOUT_S):
    """Start a fixed-frame-count burst and block until it has finished sending.

    Traffic is configured as a fixed burst (9999 frames), so it auto-stops; poll
    Traffic.State until it leaves 'started*', then let stats settle. Returns the
    final Traffic.State string."""
    logger.info("IxNetwork: starting fixed-count burst (%s)", label)
    ixnetwork.Traffic.StartStatelessTrafficBlocking()
    deadline = time.time() + timeout
    state = str(ixnetwork.Traffic.State or "")
    while time.time() < deadline:
        state = str(ixnetwork.Traffic.State or "")
        if not state.lower().startswith("started"):
            break
        time.sleep(2)
    time.sleep(BURST_STATS_SETTLE_S)   # let Flow Statistics catch up
    logger.info("IxNetwork: burst '%s' done (state=%s)", label, ixnetwork.Traffic.State)
    return state


def _parse_ixn_timestamp(ts):
    """Parse an IxNetwork timestamp 'HH:MM:SS.mmm.uuu.nnn' into float seconds. '' → None."""
    if not ts or not str(ts).strip():
        return None
    ts = str(ts).strip()
    try:
        hms, _, frac = ts.partition(".")
        h, m, s = (int(x) for x in hms.split(":"))
        base = h * 3600 + m * 60 + s
        if frac:
            digits = frac.replace(".", "")
            base += int(digits) / (10 ** len(digits))
        return float(base)
    except (ValueError, AttributeError):
        return None


def _read_flow_stats(ixnetwork, vlan_base, vlan_span=1000):
    """Per-ENI flow stats from the Flow Statistics view, keyed by global ENI index.

    Only rows whose VLAN is in [vlan_base, vlan_base+vlan_span) are returned, so
    the outbound (VLAN 1001+) and inbound (VLAN 1+) traffic items don't pollute
    each other's aggregates."""
    views = ixnetwork.Statistics.View.find(Caption="Flow Statistics")
    if len(views) == 0:
        return {}
    data = views[0].Data
    captions = list(data.ColumnCaptions)

    def col(*subs):
        for i, c in enumerate(captions):
            cl = c.lower()
            if all(s.lower() in cl for s in subs):
                return i
        return None

    ci_vlan = col("vlan", "id")
    ci_tx = col("tx frames")
    ci_rx = col("rx frames")
    ci_loss = col("loss", "%")
    ci_first = col("first", "timestamp")

    def _i(x):
        try:
            return int(float(str(x).replace(",", "")))
        except (ValueError, TypeError):
            return 0

    def _f(x):
        try:
            return float(str(x).replace(",", ""))
        except (ValueError, TypeError):
            return 0.0

    result = {}
    for page in range(1, (data.TotalPages or 1) + 1):
        if data.CurrentPage != page:
            data.CurrentPage = page
        for raw in data.PageValues:
            cells = raw[0] if (raw and isinstance(raw[0], list)) else raw
            if ci_vlan is None:
                continue
            vlan = _i(cells[ci_vlan])
            if vlan <= 0 or not (vlan_base <= vlan < vlan_base + vlan_span):
                continue
            result[vlan - vlan_base] = {
                "vlan": vlan,
                "tx": _i(cells[ci_tx]) if ci_tx is not None else 0,
                "rx": _i(cells[ci_rx]) if ci_rx is not None else 0,
                "loss_pct": _f(cells[ci_loss]) if ci_loss is not None else 0.0,
                "first_ts": _parse_ixn_timestamp(cells[ci_first]) if ci_first is not None else None,
            }
    return result


def _aggregate_loss_pct(stats):
    tx = sum(s["tx"] for s in stats.values())
    rx = sum(s["rx"] for s in stats.values())
    if tx == 0:
        return 100.0
    return max(0.0, 100.0 * (tx - rx) / tx)


# ───────────────────────────── switch counters (duthost) ───────────────────
def _collect_switch_counters(host, label):
    """Snapshot 'show interface counters' as {iface: row-dict}."""
    try:
        rows = host.show_and_parse("show interface counters")
    except Exception:
        logger.exception("  switch counters (%s) collection failed", label)
        return {}
    out = {}
    for r in rows:
        iface = r.get("iface") or r.get("interface") or r.get("port") or ""
        if iface:
            out[iface] = r
    return out


def _log_switch_delta(before, after, label):
    """Log non-zero rx_ok/tx_ok deltas between two counter snapshots."""
    def _i(x):
        try:
            return int(str(x).replace(",", ""))
        except (ValueError, TypeError):
            return 0
    logger.info("  switch interface counters delta — %s:", label)
    logger.info("    %-18s  %12s  %12s  %10s  %10s", "iface", "rx_ok Δ", "tx_ok Δ", "rx_err", "tx_err")
    any_row = False
    for iface in sorted(after):
        a = after[iface]
        b = before.get(iface, {})
        rxd = _i(a.get("rx_ok")) - _i(b.get("rx_ok"))
        txd = _i(a.get("tx_ok")) - _i(b.get("tx_ok"))
        if rxd or txd:
            any_row = True
            logger.info("    %-18s  %12d  %12d  %10s  %10s",
                        iface, rxd, txd, a.get("rx_err", "-"), a.get("tx_err", "-"))
    if not any_row:
        logger.info("    (no interfaces with rx/tx delta)")


# Diagram input: per-hop RX/TX DELTAS over one traffic run. Consumed by
# c:\tmp\make_dash_chain_chart.py (mirror to tests/dash/dash_perhop_delta.json).
_PERHOP_DELTA_JSON = os.path.join(os.path.dirname(__file__), "dash_perhop_delta.json")
# Full raw-stats companion for the diagram (IxN/UHD/NPU/DPU/NASA), one per run.
_RUN_DETAILS_TXT = os.path.join(os.path.dirname(__file__), "dash_perhop_details.txt")

# NASA exposes its port/global stats via the syncd CLI fed over stdin.
_NASA_STATS_CMD = ("printf 'port_stats_dump stats_mode READ\\nquit\\n' | "
                   "docker exec -i syncd python /usr/sbin/cli/nasa_cli.py -u 2>&1")
_NASA_STAT_RE = re.compile(r"(SAI_PORT_STAT_\w+):\s*(\d+)")


def _collect_nasa_stats(dpuhost, label):
    """Return {counter_name: int} for ALL NASA port/global stats (not just drops)."""
    try:
        out = dpuhost.shell(_NASA_STATS_CMD, module_ignore_errors=True).get("stdout", "")
    except Exception:
        logger.exception("  NASA stats (%s) collection failed", label)
        return {}
    stats = {}
    for line in out.splitlines():
        m = _NASA_STAT_RE.search(line)
        if m:
            stats[m.group(1)] = int(m.group(2))
    if not stats:
        logger.warning("  NASA stats (%s): no counters parsed (syncd/nasa_cli reachable?)", label)
    return stats


def _i(x):
    try:
        return int(str(x).replace(",", ""))
    except (ValueError, TypeError):
        return 0


def _emit_perhop_delta_json(sw_before, sw_after, dpu_before, dpu_after, flow_stats,
                            duration_s, nasa_before=None, nasa_after=None,
                            uhd_before=None, uhd_after=None, inbound_stats=None,
                            out_path=_PERHOP_DELTA_JSON):
    """Write per-hop RX/TX deltas (after-before) + NASA counter deltas for the diagram.

    Raw 'show interface counters' / NASA stats are cumulative since boot and
    polluted by control traffic + earlier debug runs — the per-run DELTA is the
    only honest signal. NPU Ethernet0 = UHD side, Ethernet224 = DPU side;
    DPU0 Ethernet0 = NPU side.
    """
    def _delta(before, after, iface):
        a, b = after.get(iface, {}), before.get(iface, {})
        return {"rx": _i(a.get("rx_ok")) - _i(b.get("rx_ok")),
                "tx": _i(a.get("tx_ok")) - _i(b.get("tx_ok"))}

    nasa_before = nasa_before or {}
    nasa_after = nasa_after or {}
    nasa_delta = {k: nasa_after.get(k, 0) - nasa_before.get(k, 0)
                  for k in set(nasa_before) | set(nasa_after)}
    # Dominant DROP stage over the run: largest positive *_DROP_PACKETS delta.
    drops = {k: v for k, v in nasa_delta.items() if "DROP_PACKETS" in k and v > 0}
    dom_drop, dom_drop_n = (max(drops.items(), key=lambda kv: kv[1]) if drops else (None, 0))

    payload = {
        "captured_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "duration_s": int(duration_s),
        "npu": {"Ethernet0": _delta(sw_before, sw_after, "Ethernet0"),
                "Ethernet224": _delta(sw_before, sw_after, "Ethernet224")},
        "dpu": {"Ethernet0": _delta(dpu_before, dpu_after, "Ethernet0")},
        "ixia": {"tx": sum(s.get("tx", 0) for s in flow_stats.values()),
                 "rx": sum(s.get("rx", 0) for s in flow_stats.values())},
        "ixia_inbound": {"tx": sum(s.get("tx", 0) for s in (inbound_stats or {}).values()),
                         "rx": sum(s.get("rx", 0) for s in (inbound_stats or {}).values())},
        "nasa_delta": nasa_delta,
        "dominant_drop": {"counter": dom_drop, "packets": dom_drop_n},
    }
    # UHD per-port frame deltas (rx/tx-all) — the appliance's own view of the loop.
    uhd_b, uhd_a = uhd_before or {}, uhd_after or {}
    uhd_d = {}
    for port in set(uhd_b) | set(uhd_a):
        b, a = uhd_b.get(port, {}), uhd_a.get(port, {})
        uhd_d[port] = {"rx": _i(a.get("frames_received_all")) - _i(b.get("frames_received_all")),
                       "tx": _i(a.get("frames_transmitted_all")) - _i(b.get("frames_transmitted_all"))}
    if uhd_d:
        payload["uhd"] = uhd_d
    # Single-line copy in the log so the deltas survive even if file sync is manual.
    logger.info("PERHOP_DELTA_JSON=%s", json.dumps(payload, separators=(",", ":")))
    if dom_drop:
        logger.info("  NASA dominant drop this run: %s = %d", dom_drop, dom_drop_n)
    try:
        with open(out_path, "w") as f:
            json.dump(payload, f, indent=2)
        logger.info("  wrote per-hop delta JSON: %s", out_path)
    except OSError:
        logger.exception("  failed to write per-hop delta JSON to %s", out_path)
    return payload


def _fmt_counter_table(before, after, keys, b_label="before", a_label="after"):
    """Format a before/after/delta table for arbitrary {name: int(-ish)} dicts."""
    lines = ["    %-52s %16s %16s %16s" % ("counter", b_label, a_label, "delta")]
    for k in keys:
        b, a = _i(before.get(k, 0)), _i(after.get(k, 0))
        lines.append("    %-52s %16d %16d %16d" % (k, b, a, a - b))
    return "\n".join(lines)


def _uhd_delta(uhd_before, uhd_after):
    """{port: {metric: (before, after, delta)}} for numeric UHD metrics."""
    out = {}
    for port in sorted(set(uhd_before or {}) | set(uhd_after or {})):
        b, a = (uhd_before or {}).get(port, {}), (uhd_after or {}).get(port, {})
        row = {}
        for m in sorted(set(b) | set(a)):
            bv, av = b.get(m), a.get(m)
            try:
                row[m] = (int(bv), int(av), int(av) - int(bv))
            except (ValueError, TypeError):
                row[m] = (bv, av, None)   # non-numeric (e.g. link_status)
        out[port] = row
    return out


def _write_run_details(payload, sw_before, sw_after, dpu_before, dpu_after,
                       nasa_before, nasa_after, uhd_before, uhd_after, flow_stats,
                       inbound_stats=None, out_path=_RUN_DETAILS_TXT):
    """Drop EVERY raw stat behind the run's diagram into one companion text file:
    IxN HW/flow stats, UHD per-port (before/after/delta), NPU + DPU interface
    counters (before/after/delta), and the full NASA dump (before/after/delta)."""
    sec = []
    sec.append("DASH traffic-path run details — %s (duration %ss)"
               % (payload.get("captured_utc"), payload.get("duration_s")))
    dd = payload.get("dominant_drop", {})
    sec.append("dominant NASA drop this run: %s = %s"
               % (dd.get("counter"), dd.get("packets")))
    sec.append("")

    sec.append("== IxNetwork flow stats (per-ENI) ==")
    sec.append("    %-8s %14s %14s %10s" % ("eni", "tx", "rx", "loss%"))
    tot_tx = tot_rx = 0
    for g in sorted(flow_stats):
        s = flow_stats[g]
        tot_tx += s.get("tx", 0)
        tot_rx += s.get("rx", 0)
        sec.append("    %-8s %14d %14d %10.3f"
                   % (g, s.get("tx", 0), s.get("rx", 0), s.get("loss_pct", 0.0)))
    agg = 100.0 * (tot_tx - tot_rx) / tot_tx if tot_tx else 0.0
    sec.append("    TOTAL    tx=%d rx=%d  aggregate loss=%.3f%%" % (tot_tx, tot_rx, agg))
    sec.append("")

    sec.append("== IxNetwork INBOUND IPv6 flow stats (service->VM) ==")
    itx = irx = 0
    for g in sorted(inbound_stats or {}):
        s = inbound_stats[g]
        itx += s.get("tx", 0)
        irx += s.get("rx", 0)
        sec.append("    eni %-6s vlan %-5s tx %-10d rx %-10d loss %.3f%%"
                   % (g, s.get("vlan"), s.get("tx", 0), s.get("rx", 0), s.get("loss_pct", 0.0)))
    iagg = 100.0 * (itx - irx) / itx if itx else 0.0
    sec.append("    TOTAL    tx=%d rx=%d  aggregate loss=%.3f%%" % (itx, irx, iagg))
    if not inbound_stats:
        sec.append("    (no inbound flows)")
    sec.append("")

    sec.append("== UHD per-port metrics (before / after / delta) ==")
    ud = _uhd_delta(uhd_before, uhd_after)
    for port in sorted(ud):
        sec.append("    %s:" % port)
        sec.append("        %-28s %16s %16s %16s" % ("metric", "before", "after", "delta"))
        for m in sorted(ud[port]):
            bv, av, dv = ud[port][m]
            sec.append("        %-28s %16s %16s %16s"
                       % (m, bv, av, "" if dv is None else dv))
    if not ud:
        sec.append("    (no UHD metrics)")
    sec.append("")

    sec.append("== NPU interface counters (before / after / delta) ==")
    sec.append(_fmt_counter_table(
        {k: sw_before.get(k, {}).get("rx_ok") for k in ("Ethernet0", "Ethernet224")},
        {k: sw_after.get(k, {}).get("rx_ok") for k in ("Ethernet0", "Ethernet224")},
        ["Ethernet0", "Ethernet224"], "rx_before", "rx_after"))
    sec.append(_fmt_counter_table(
        {k: sw_before.get(k, {}).get("tx_ok") for k in ("Ethernet0", "Ethernet224")},
        {k: sw_after.get(k, {}).get("tx_ok") for k in ("Ethernet0", "Ethernet224")},
        ["Ethernet0", "Ethernet224"], "tx_before", "tx_after"))
    sec.append("")

    sec.append("== DPU0 interface counters (before / after / delta) ==")
    sec.append(_fmt_counter_table(
        {"Ethernet0": dpu_before.get("Ethernet0", {}).get("rx_ok")},
        {"Ethernet0": dpu_after.get("Ethernet0", {}).get("rx_ok")},
        ["Ethernet0"], "rx_before", "rx_after"))
    sec.append(_fmt_counter_table(
        {"Ethernet0": dpu_before.get("Ethernet0", {}).get("tx_ok")},
        {"Ethernet0": dpu_after.get("Ethernet0", {}).get("tx_ok")},
        ["Ethernet0"], "tx_before", "tx_after"))
    sec.append("")

    sec.append("== NASA port_stats_dump (ALL counters, before / after / delta) ==")
    nkeys = sorted(set(nasa_before or {}) | set(nasa_after or {}))
    sec.append(_fmt_counter_table(nasa_before or {}, nasa_after or {}, nkeys))
    sec.append("")

    text = "\n".join(sec)
    try:
        with open(out_path, "w") as f:
            f.write(text + "\n")
        logger.info("  wrote run details: %s", out_path)
    except OSError:
        logger.exception("  failed to write run details to %s", out_path)
    return text


# ───────────────────────── results / correlation table ─────────────────────
def _print_traffic_vs_grpc(eni_indices, push_events, flow_stats):
    """Compare per-ENI gNMI push-complete deltas vs traffic first-seen deltas."""
    grpc_ts = {}
    for ev in push_events.values():
        if ev["kind"] in ("map", "eni") and ev["idx"] is not None:
            cur = grpc_ts.get(ev["idx"])
            if cur is None or ev["kind"] == "map":
                grpc_ts[ev["idx"]] = ev["end"]

    rows = []
    for idx in eni_indices:
        st = flow_stats.get(idx, {})
        rows.append({
            "idx": idx, "grpc_ts": grpc_ts.get(idx), "traffic_ts": st.get("first_ts"),
            "rx": st.get("rx", 0), "loss": st.get("loss_pct", 100.0),
        })

    grpc_vals = [r["grpc_ts"] for r in rows if r["grpc_ts"] is not None]
    traf_vals = [r["traffic_ts"] for r in rows if r["traffic_ts"] is not None]
    grpc0 = min(grpc_vals) if grpc_vals else 0.0
    traf0 = min(traf_vals) if traf_vals else 0.0

    sep = "=" * 96
    logger.info(sep)
    logger.info("  DASH API SPEED — gNMI push vs. live-traffic per-ENI bring-up")
    logger.info(sep)
    logger.info("  Times are relative to the first ENI in each column (seconds).")
    logger.info("  %-5s  %10s  %10s  %12s  %12s  %10s  %8s",
                "ENI", "gNMI t", "Traffic t", "gNMI Δ/eni", "Traf Δ/eni", "Rx frames", "Loss %")
    logger.info("  " + "-" * 92)

    prev_g = prev_t = None
    n_forwarding = 0
    grpc_deltas = []
    traf_deltas = []
    for r in rows:
        g_rel = (r["grpc_ts"] - grpc0) if r["grpc_ts"] is not None else None
        t_rel = (r["traffic_ts"] - traf0) if r["traffic_ts"] is not None else None
        g_d = (g_rel - prev_g) if (g_rel is not None and prev_g is not None) else None
        t_d = (t_rel - prev_t) if (t_rel is not None and prev_t is not None) else None
        if g_d is not None:
            grpc_deltas.append(g_d)
        if t_d is not None:
            traf_deltas.append(t_d)
        if r["traffic_ts"] is not None:
            n_forwarding += 1
        logger.info("  %-5d  %10s  %10s  %12s  %12s  %10d  %8.2f",
                    r["idx"],
                    "%.3f" % g_rel if g_rel is not None else "-",
                    "%.3f" % t_rel if t_rel is not None else "-",
                    "%.3f" % g_d if g_d is not None else "-",
                    "%.3f" % t_d if t_d is not None else "-",
                    r["rx"], r["loss"])
        if g_rel is not None:
            prev_g = g_rel
        if t_rel is not None:
            prev_t = t_rel

    logger.info("  " + "-" * 92)

    def _avg(xs):
        return (sum(xs) / len(xs)) if xs else 0.0

    logger.info("  ENIs programmed (gNMI):     %d", len(grpc_ts))
    logger.info("  ENIs forwarding (traffic):  %d / %d", n_forwarding, len(eni_indices))
    logger.info("  Avg gNMI   time / ENI:      %.3f s", _avg(grpc_deltas))
    logger.info("  Avg traffic time / ENI:     %.3f s", _avg(traf_deltas))
    if grpc_vals:
        logger.info("  gNMI   total span:          %.3f s", max(grpc_vals) - grpc0)
    if traf_vals:
        logger.info("  Traffic total span:         %.3f s", max(traf_vals) - traf0)
    logger.info(sep)
    return n_forwarding


# ─────────────────────────────────── test ──────────────────────────────────
def test_dash_api_load_speed_pl_with_traffic(localhost, duthost, dpuhosts, dpu_index, config_facts, creds):
    """Push DASH configs under continuous IxNetwork traffic; correlate gNMI vs HW bring-up."""
    dpuhost = dpuhosts[dpu_index]

    dpu_name = f"DPU{dpuhost.dpu_index}"
    dpu_midplane_ip = "169.254.200.%d" % (dpuhost.dpu_index + 1)
    logger.info("Pre-flight: assuming %s is up at %s (no automated check)", dpu_name, dpu_midplane_ip)

    # ── Render configs ──────────────────────────────────────────────────────
    # ENIs per DPU depends on platform: Nvidia DPU holds 64, Cisco 32. render's
    # enis_per_dpu = ENI_COUNT // DPUS (DEFAULTS 256/8 = 32). Keep ENI_COUNT=256
    # (so per-ENI route count = 500 stays matched to the UHD/bg.ixncfg dataplane)
    # and set DPUS=4 -> 64 ENIs/DPU for Nvidia, DPUS=8 -> 32 for Cisco.
    hwsku = duthost.facts.get("hwsku", "")
    params = dict(render.DEFAULTS)
    params["DPUS"] = 8 if "Cisco" in hwsku else 4
    # Minimal config: exactly ONE VNet mapping (1.4.0.1) + ONE outbound route per
    # ENI (instead of 64k mappings / ~500 routes) to isolate single-flow forwarding.
    params["MINIMAL_SINGLE_ENTRY"] = True
    enis_per_dpu = params["ENI_COUNT"] // params["DPUS"]
    render_output_dir = tempfile.mkdtemp(prefix="dash_cfg_", dir=os.path.dirname(os.path.abspath(__file__)))
    logger.info("Rendering DASH configs (hwsku=%s, DPUS=%d -> %d ENIs/DPU = %d files) into %s",
                hwsku, params["DPUS"], enis_per_dpu, 1 + 2 * enis_per_dpu, render_output_dir)
    render.generate(params, render_output_dir, prefix="pl_100")

    config_dir = os.path.join(render_output_dir, f"dpu{dpuhost.dpu_index}")
    assert os.path.isdir(config_dir), f"Config directory not found after render: {config_dir}"

    pattern = f"*dpu{dpuhost.dpu_index}*.json"
    files = sorted(f for f in os.listdir(config_dir) if fnmatch.fnmatch(f, pattern) and f.endswith(".json"))
    assert files, f"No JSON config files found matching '{pattern}' in {config_dir}"

    if _ENI_COUNT != "ALL":
        n = int(_ENI_COUNT)
        filtered = []
        for f in files:
            m = re.search(r"\.(\d{3})(apl|eni|map)\.json$", f)
            if m and int(m.group(1)) < n:
                filtered.append(f)
        assert filtered, f"_ENI_COUNT={_ENI_COUNT} filtered out all files (had {len(files)})"
        logger.info("_ENI_COUNT=%s: pushing %d/%d rendered files", _ENI_COUNT, len(filtered), len(files))
        files = filtered

    # Global ENI indices we will program. The rendered filename index IS the
    # global ENI index, and the IxNetwork outbound VLAN is VLAN_OUT_BASE + index.
    eni_indices = sorted({
        idx for f in files
        for idx, kind in [parse_file_index(f)]
        if idx is not None and kind in ("eni", "map")
    })
    assert eni_indices, "No ENI/map files found to derive per-ENI flows"
    logger.info("Will program %d ENIs (global indices %d..%d, VLANs %d..%d), %d config files",
                len(eni_indices), eni_indices[0], eni_indices[-1],
                VLAN_OUT_BASE + eni_indices[0], VLAN_OUT_BASE + eni_indices[-1], len(files))

    if "Cisco" in hwsku:
        dpu_dataplane_ip = "18.%d.202.1" % dpuhost.dpu_index
    else:
        dpu_dataplane_ip = "10.0.0.%d" % (57 + dpuhost.dpu_index * 2)

    mem_before = {"NPU": _collect_memory(duthost), "DPU": _collect_memory(dpuhost)}
    redis_before = _collect_redis_memory(dpuhost)

    dpu_pre_config(dpuhost, dpu_dataplane_ip)
    npu_pre_config(duthost, dpu_midplane_ip, dpu_dataplane_ip)

    # ── IxNetwork: build the target DPU's outbound traffic live (no .ixncfg) ─
    session, ixnetwork = _ix_connect()
    timings = {}
    mem_timeline = []
    push_events = {}
    flow_stats = {}
    inbound_stats = {}
    sw_before = {}
    sw_after = {}
    dpu_before = {}
    dpu_after = {}
    nasa_before = {}
    nasa_after = {}
    uhd_before = {}
    uhd_last = {}
    traffic_t0 = None
    traffic_dur = 0.0
    total_start = time.time()
    traffic_started = False

    try:
        build_outbound_config(ixnetwork, dpuhost.dpu_index, enis_per_dpu=len(eni_indices))
        # Inbound (service->VM) IPv6 flow on the same DPU/ENI — sent alongside the
        # outbound burst so we can see which direction forwards and which drops.
        build_inbound_config(ixnetwork, dpuhost.dpu_index, enis_per_dpu=len(eni_indices))
        states = {vp.Name: vp.State for vp in ixnetwork.Vport.find()}
        logger.info("IxNetwork port states: %s", states)

        # ── Baseline burst (no ENIs programmed) → expect ~100% loss ──────────
        dash_uhd_stats.clear_metrics(UHD_IP)
        ixnetwork.ClearStats()
        _ix_run_fixed_burst(ixnetwork, "baseline")
        traffic_started = True
        baseline_stats = _read_flow_stats(ixnetwork, VLAN_OUT_BASE)
        baseline_loss = _aggregate_loss_pct(baseline_stats)
        logger.info("Baseline burst: aggregate loss %.2f%% across %d flows (expect >= %.1f%%)",
                    baseline_loss, len(baseline_stats), BASELINE_MIN_LOSS_PCT)
        if baseline_loss < BASELINE_MIN_LOSS_PCT:
            logger.warning("Baseline loss %.2f%% below %.1f%% — some flows already forwarding "
                           "(stale config?).", baseline_loss, BASELINE_MIN_LOSS_PCT)

        # ── Program all ENIs via gNMI ────────────────────────────────────────
        def _log_progress(filename, idx, kind, t0, t1):
            if kind == "map":
                logger.info("    gNMI: ENI %s programmed (%.2fs)", idx, t1 - t0)

        logger.info("Programming %d config files via gNMI ...", len(files))
        load_json_via_gnmi(localhost, duthost, dpuhost, config_facts, config_dir, files, timings,
                           creds, mem_timeline, push_events=push_events, on_file_done=_log_progress)

        # ── Measurement burst (post-program): fixed count, deltas around it ──
        logger.info("Settling %ds for ASIC bring-up before the measurement burst ...",
                    POST_PROGRAM_SETTLE_S)
        time.sleep(POST_PROGRAM_SETTLE_S)

        # Every "before" snapshot is taken immediately before the measurement
        # burst so each delta reflects exactly that one fixed (9999-frame) burst.
        dash_uhd_stats.clear_metrics(UHD_IP)
        ixnetwork.ClearStats()
        time.sleep(2)
        sw_before = _collect_switch_counters(duthost, "measure-before-NPU")
        dpu_before = _collect_switch_counters(dpuhost, "measure-before-DPU")
        nasa_before = _collect_nasa_stats(dpuhost, "measure-before-NASA")
        uhd_before = dash_uhd_stats.query_metrics(UHD_IP, UHD_PORT_NAMES)

        traffic_t0 = time.time()
        _ix_run_fixed_burst(ixnetwork, "measurement")
        traffic_dur = time.time() - traffic_t0

        sw_after = _collect_switch_counters(duthost, "measure-after-NPU")
        dpu_after = _collect_switch_counters(dpuhost, "measure-after-DPU")
        nasa_after = _collect_nasa_stats(dpuhost, "measure-after-NASA")
        uhd_last = dash_uhd_stats.query_metrics(UHD_IP, UHD_PORT_NAMES)
        dash_uhd_stats.log_uhd_table(UHD_IP, UHD_PORT_NAMES, label="measurement", prev=uhd_before)
        flow_stats = _read_flow_stats(ixnetwork, VLAN_OUT_BASE)
        inbound_stats = _read_flow_stats(ixnetwork, VLAN_IN_BASE)
        logger.info("Measurement burst OUTBOUND(IPv4 1.1.0.1->1.4.0.1): %d flows, "
                    "aggregate loss %.2f%% (tx should be %d/flow)",
                    len(flow_stats), _aggregate_loss_pct(flow_stats), OUTBOUND_FRAME_COUNT)
        logger.info("Measurement burst INBOUND (IPv6 of 1.4.0.1->1.1.0.1): %d flows, "
                    "aggregate loss %.2f%% (tx should be %d/flow)",
                    len(inbound_stats), _aggregate_loss_pct(inbound_stats), INBOUND_FRAME_COUNT)
        for g, s in sorted(inbound_stats.items()):
            logger.info("    inbound ENI %d (vlan %d): tx=%d rx=%d loss=%.2f%%",
                        g, s.get("vlan"), s.get("tx", 0), s.get("rx", 0), s.get("loss_pct", 0.0))
    finally:
        if traffic_started:
            _ix_stop_traffic(ixnetwork)
        try:
            session.Session.remove()
        except Exception:
            logger.debug("IxNetwork: session handle release skipped (non-fatal)")

        shutil.rmtree(render_output_dir, ignore_errors=True)
        logger.info("Cleaned up rendered config dir: %s", render_output_dir)

        total_elapsed = time.time() - total_start
        try:
            mem_after = {"NPU": _collect_memory(duthost), "DPU": _collect_memory(dpuhost)}
            redis_after = _collect_redis_memory(dpuhost)
            _print_results(timings, total_elapsed, mem_before, mem_after, redis_before, redis_after, mem_timeline)
        except Exception:
            logger.exception("Failed to collect/print post-test memory results")

    # ── Switch + correlation reporting ──────────────────────────────────────
    if sw_before and sw_after:
        _log_switch_delta(sw_before, sw_after, "NPU (push window)")
    if dpu_before and dpu_after:
        _log_switch_delta(dpu_before, dpu_after, "DPU0 (push window)")
    # Emit the per-hop DELTA JSON (incl. NASA deltas + dominant drop) and the full
    # raw-stats details file the diagram is built from.
    try:
        payload = _emit_perhop_delta_json(sw_before, sw_after, dpu_before, dpu_after,
                                          flow_stats, traffic_dur,
                                          nasa_before=nasa_before, nasa_after=nasa_after,
                                          uhd_before=uhd_before, uhd_after=uhd_last,
                                          inbound_stats=inbound_stats)
        _write_run_details(payload, sw_before, sw_after, dpu_before, dpu_after,
                           nasa_before, nasa_after, uhd_before, uhd_last, flow_stats,
                           inbound_stats=inbound_stats)
    except Exception:
        logger.exception("Failed to emit per-hop delta JSON / run details")
    n_forwarding = _print_traffic_vs_grpc(eni_indices, push_events, flow_stats)

    # ── Assertions ──────────────────────────────────────────────────────────
    final_loss = _aggregate_loss_pct(flow_stats)
    assert n_forwarding >= 1, (
        "No flow ever started forwarding — no ENI brought up traffic. "
        "Check UHD config (smartswitch loaded?), chassis links, VLAN↔ENI mapping."
    )
    logger.info("Flows forwarding: %d / %d, final aggregate loss %.2f%%",
                n_forwarding, len(eni_indices), final_loss)
    assert n_forwarding >= len(eni_indices), (
        f"Only {n_forwarding}/{len(eni_indices)} ENIs started forwarding traffic — "
        "some ENIs never came up in hardware."
    )
    assert final_loss <= SETTLE_LOSS_PCT, (
        "Aggregate loss settled at %.2f%% (> %s%%) after the full config was pushed — "
        "traffic did not fully recover." % (final_loss, SETTLE_LOSS_PCT)
    )
