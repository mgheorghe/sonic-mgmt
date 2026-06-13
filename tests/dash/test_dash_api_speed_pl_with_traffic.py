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
from dash_traffic_ixn_build import build_outbound_config, VLAN_OUT_BASE

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
_ENI_COUNT = "ALL"

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

# Baseline (pre-program) loss check.
BASELINE_SETTLE_S = 5
BASELINE_MIN_LOSS_PCT = 99.0

# Post-push settle: poll until aggregate loss stops improving (or threshold).
SETTLE_POLL_INTERVAL_S = 3
SETTLE_TIMEOUT_S = 90
SETTLE_LOSS_PCT = 1.0
SETTLE_STABLE_POLLS = 3
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


def _read_flow_stats(ixnetwork, vlan_base):
    """Per-ENI flow stats from the Flow Statistics view, keyed by global ENI index."""
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
            if vlan <= 0:
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

    # ── Render configs (identical to the gRPC-only speed test) ──────────────
    render_output_dir = tempfile.mkdtemp(prefix="dash_cfg_", dir=os.path.dirname(os.path.abspath(__file__)))
    logger.info("Rendering DASH configs into %s", render_output_dir)
    render.generate(dict(render.DEFAULTS), render_output_dir, prefix="pl_100")

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

    hwsku = duthost.facts.get("hwsku", "")
    logger.info("NPU hwsku: %s", hwsku)
    if "Cisco" in hwsku:
        dpu_dataplane_ip = "18.%d.202.1" % dpuhost.dpu_index
    else:
        dpu_dataplane_ip = "10.0.0.%d" % (57 + dpuhost.dpu_index * 2)

    mem_before = {"NPU": _collect_memory(duthost), "DPU": _collect_memory(dpuhost)}
    redis_before = _collect_redis_memory(dpuhost)

    dpu_pre_config(dpuhost)
    npu_pre_config(duthost, dpu_midplane_ip, dpu_dataplane_ip)

    # ── IxNetwork: build the target DPU's outbound traffic live (no .ixncfg) ─
    session, ixnetwork = _ix_connect()
    timings = {}
    mem_timeline = []
    push_events = {}
    flow_stats = {}
    sw_before = {}
    sw_after = {}
    total_start = time.time()
    traffic_started = False

    try:
        build_outbound_config(ixnetwork, dpuhost.dpu_index)
        states = {vp.Name: vp.State for vp in ixnetwork.Vport.find()}
        logger.info("IxNetwork port states: %s", states)

        # Baseline counter snapshots before traffic/programming.
        dash_uhd_stats.clear_metrics(UHD_IP)
        sw_before = _collect_switch_counters(duthost, "baseline-NPU")
        uhd_before = dash_uhd_stats.log_uhd_table(UHD_IP, UHD_PORT_NAMES, label="baseline")

        _ix_start_traffic(ixnetwork)
        traffic_started = True

        # Baseline: with no ENIs programmed we expect ~100% loss.
        logger.info("Baseline: letting traffic run %ds before programming ...", BASELINE_SETTLE_S)
        time.sleep(BASELINE_SETTLE_S)
        baseline_stats = _read_flow_stats(ixnetwork, VLAN_OUT_BASE)
        baseline_loss = _aggregate_loss_pct(baseline_stats)
        logger.info("Baseline aggregate loss: %.2f%% across %d flows (expected >= %.1f%%)",
                    baseline_loss, len(baseline_stats), BASELINE_MIN_LOSS_PCT)
        dash_uhd_stats.log_uhd_table(UHD_IP, UHD_PORT_NAMES, label="after traffic start", prev=uhd_before)
        if baseline_loss < BASELINE_MIN_LOSS_PCT:
            logger.warning("Baseline loss %.2f%% below %.1f%% — some flows already forwarding "
                           "(stale config?). Continuing, but per-ENI deltas may be skewed.",
                           baseline_loss, BASELINE_MIN_LOSS_PCT)

        # ── Push all ENIs via gNMI while traffic runs ───────────────────────
        def _log_progress(filename, idx, kind, t0, t1):
            if kind == "map":
                logger.info("    gNMI: ENI %s programmed (%.2fs)", idx, t1 - t0)

        logger.info("Programming %d config files via gNMI (traffic running) ...", len(files))
        load_json_via_gnmi(localhost, duthost, dpuhost, config_facts, config_dir, files, timings,
                           creds, mem_timeline, push_events=push_events, on_file_done=_log_progress)

        # ── Settle: poll until loss converges or stops improving ────────────
        logger.info("Push done — polling traffic until loss converges (timeout %ds) ...", SETTLE_TIMEOUT_S)
        deadline = time.time() + SETTLE_TIMEOUT_S
        last_loss = None
        stable = 0
        flow_stats = baseline_stats
        while time.time() < deadline:
            time.sleep(SETTLE_POLL_INTERVAL_S)
            flow_stats = _read_flow_stats(ixnetwork, VLAN_OUT_BASE)
            loss = _aggregate_loss_pct(flow_stats)
            fwd = sum(1 for s in flow_stats.values() if s.get("first_ts") is not None)
            logger.info("  settle: aggregate loss=%.2f%%  flows forwarding=%d/%d",
                        loss, fwd, len(eni_indices))
            if loss <= SETTLE_LOSS_PCT:
                logger.info("  settle: loss <= %.1f%% — converged", SETTLE_LOSS_PCT)
                break
            if last_loss is not None and abs(last_loss - loss) < 0.05:
                stable += 1
                if stable >= SETTLE_STABLE_POLLS:
                    logger.info("  settle: loss flat for %d polls — stopping", stable)
                    break
            else:
                stable = 0
            last_loss = loss

        # Post-run counter snapshots.
        sw_after = _collect_switch_counters(duthost, "after-NPU")
        dash_uhd_stats.log_uhd_table(UHD_IP, UHD_PORT_NAMES, label="after settle", prev=uhd_before)
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
