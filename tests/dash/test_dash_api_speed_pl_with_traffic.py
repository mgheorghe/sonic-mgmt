"""DASH API load-speed test *with live traffic* (IxNetwork / RestPy).

Same config-push as ``test_dash_api_speed_pl.py`` (it reuses
``dash_api_speed_common.load_json_via_gnmi``), but with continuous IxNetwork
traffic running across the whole push so we can measure how fast each ENI
*actually* starts forwarding in hardware — and compare that against how fast
gRPC reported the config as pushed.

How it works
------------
* A **known-good IxNetwork config** (``bg.ixncfg``) and the matching **UHD
  config** (``smartswitch-nvidia.http``) live in the sibling directory
  ``test_dash_api_speed_pl_with_traffic/``. The UHD (Keysight "connect" fabric)
  bridges IxNetwork VLAN traffic ↔ DUT VXLAN/NVGRE: IxNetwork sends VLAN-tagged
  frames, the DPU encaps/decaps them, and they loop back VLAN-tagged.
* ``bg.ixncfg`` holds 16 raw traffic items — ``DPU<N>-Out`` (VXLAN/outbound) and
  ``DPU<N>-In`` (NVGRE/inbound) for DPU 0..7. Each TI has **32 flows, one per
  ENI**, on a unique VLAN: outbound flow ``i`` (ENI ``i``) uses VLAN
  ``VLAN_OUT_BASE + eni_global`` where ``eni_global = dpu_index*32 + i``. That is
  exactly the per-ENI gNMI file index, so flow ↔ ENI maps 1:1.
* This test loads ``bg.ixncfg``, **enables only the target DPU's ``-Out`` TI**
  (32 flows = 32 ENIs), tracks per-VLAN, and starts continuous traffic *before*
  any ENI is programmed — so every flow starts at ~100% loss.
* While traffic runs, all ENIs are programmed via gNMI. As each ENI lands in
  hardware its flow starts passing; the IxNetwork **"First TimeStamp"** per-flow
  statistic records when the first packet of that VLAN came back.
* ``Δ(first_ts[i+1] − first_ts[i])`` is the hardware per-ENI bring-up time
  (traffic-observed); we compare it against ``Δ(grpc_complete[i+1] −
  grpc_complete[i])`` (gNMI-observed) and print a table.
* By the time the full config is pushed, loss should have dropped to ~0.

The IxNetwork ScriptGen text dump of ``bg.ixncfg`` is checked in beside it as
``bg.scriptgen.tcl`` (see the ``ixnetwork-scriptgen`` skill for how it was made).

Run (from inside the sonic-mgmt container, like the gRPC-only speed test)::

    pytest dash/test_dash_api_speed_pl_with_traffic.py \
        --testbed=keysight-nss01 --testbed_file=../ansible/testbed.yaml \
        --inventory=../ansible/lab --host-pattern=keysight-nss01 \
        --dpu_index=0 --dpu-pattern=keysight-nss01-dpu0 --cache-clear -v
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
from dash_api_speed_common import (
    _collect_memory,
    _collect_redis_memory,
    _print_results,
    dpu_pre_config,
    load_json_via_gnmi,
    npu_pre_config,
    parse_file_index,
)

try:
    from ixnetwork_restpy import SessionAssistant
    from ixnetwork_restpy.files import Files
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
# Int N = apl + eni/map files with index 000..(N-1).
_ENI_COUNT = "ALL"

# ════════════════════════════════════════════════════════════════════════════
#  IXIA CONFIG  —  EDIT FOR YOUR TESTBED
# ════════════════════════════════════════════════════════════════════════════
# Classic IxNetwork API server (Windows, REST port 11009). Verified: 10.36.78.95
# runs IxNetwork 26.1 and loads bg.ixncfg. The bg.ixncfg ports live on chassis
# 10.36.77.138 (cards 7/8) — that chassis must be up/linked for a real run.
IXIA_API_SERVER_IP = "10.36.78.95"
IXIA_API_SERVER_PORT = 11009
IXIA_API_USER = None          # classic Windows API server needs no creds; set if yours does
IXIA_API_PASSWORD = None

# Known-good saved IxNetwork config (per-DPU raw traffic items, per-ENI VLANs).
IXNCFG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "test_dash_api_speed_pl_with_traffic", "bg.ixncfg",
)

# Per-DPU outbound (VXLAN) traffic item name, and the VLAN base.
# Outbound flow for global ENI index e uses VLAN = VLAN_OUT_BASE + e.
TRAFFIC_ITEM_TEMPLATE = "DPU{dpu}-Out"
VLAN_OUT_BASE = 1001

# Baseline (pre-program) loss check.
BASELINE_SETTLE_S = 5                  # let counters accumulate before checking
BASELINE_MIN_LOSS_PCT = 99.0           # expect ~100% loss with no ENIs

# Post-push settle: poll until aggregate loss stops improving (or threshold).
SETTLE_POLL_INTERVAL_S = 3
SETTLE_TIMEOUT_S = 90
SETTLE_LOSS_PCT = 1.0                  # consider "converged" below this loss
SETTLE_STABLE_POLLS = 3                # ...or once loss is flat this many polls
# ════════════════════════════════════════════════════════════════════════════


# ─────────────────────────── IxNetwork / RestPy helpers ────────────────────
def _ix_connect():
    """Open a RestPy session and return (SessionAssistant, IxNetwork). No ClearConfig."""
    logger.info("IxNetwork: connecting to API server %s:%s",
                IXIA_API_SERVER_IP, IXIA_API_SERVER_PORT)
    kwargs = dict(
        IpAddress=IXIA_API_SERVER_IP,
        RestPort=IXIA_API_SERVER_PORT,
        SessionName="dash-api-speed-traffic",
        ClearConfig=False,
        LogLevel=SessionAssistant.LOGLEVEL_INFO,
    )
    if IXIA_API_USER:
        kwargs["UserName"] = IXIA_API_USER
    if IXIA_API_PASSWORD:
        kwargs["Password"] = IXIA_API_PASSWORD
    sa = SessionAssistant(**kwargs)
    return sa, sa.Ixnetwork


def _ix_load_config(ixnetwork, path):
    """Upload + load a local .ixncfg onto the API server."""
    assert os.path.isfile(path), f"IxNetwork config not found: {path}"
    logger.info("IxNetwork: loading config %s", path)
    ixnetwork.LoadConfig(Files(path, local_file=True))
    tis = ixnetwork.Traffic.TrafficItem.find()
    logger.info("IxNetwork: loaded %d vports, %d traffic items",
                len(ixnetwork.Vport.find()), len(tis))


def _ix_select_traffic_item(ixnetwork, ti_name):
    """Enable only *ti_name*, disable all other TIs, track it by VLAN id. Returns the TI."""
    target = None
    for ti in ixnetwork.Traffic.TrafficItem.find():
        if ti.Name == ti_name:
            target = ti
            ti.Enabled = True
        else:
            ti.Enabled = False
    assert target is not None, (
        "Traffic item '%s' is missing from the loaded config. Available: %s"
        % (ti_name, [t.Name for t in ixnetwork.Traffic.TrafficItem.find()])
    )
    # Track per-VLAN so the Flow Statistics view has one row per ENI VLAN.
    tracking = target.Tracking.find()
    tracking.TrackBy = ["trackingenabled0", "vlanVlanId0"]
    logger.info("IxNetwork: selected TI '%s' (%d flows), tracking by VLAN id",
                ti_name, len(target.ConfigElement.find()))
    return target


def _ix_connect_ports(ixnetwork):
    """Best-effort (re)assign the config's saved chassis ports so links come up."""
    locs, hrefs = [], []
    for vp in ixnetwork.Vport.find():
        at = (vp.AssignedTo or "").strip()
        parts = at.split(":")
        if len(parts) != 3:
            continue
        ip, card, port = parts
        locs.append({"Arg1": ip, "Arg2": int(card), "Arg3": int(port)})
        hrefs.append(vp.href)
    if not locs:
        logger.warning("IxNetwork: no assigned ports found in config")
        return
    try:
        logger.info("IxNetwork: (re)assigning %d ports ...", len(locs))
        ixnetwork.AssignPorts(locs, [], hrefs, True)
    except Exception:
        logger.exception("IxNetwork: AssignPorts failed (chassis down?) — continuing")
    states = {vp.Name: vp.State for vp in ixnetwork.Vport.find()}
    down = [n for n, s in states.items() if s != "up"]
    logger.info("IxNetwork: port states: %s", states)
    if down:
        logger.warning("IxNetwork: %d port(s) not up: %s", len(down), down)


def _ix_generate_apply(ixnetwork):
    """Generate enabled traffic items and apply to hardware."""
    enabled = [t for t in ixnetwork.Traffic.TrafficItem.find() if t.Enabled]
    for ti in enabled:
        ti.Generate()
    ixnetwork.Traffic.Apply()
    logger.info("IxNetwork: generated+applied %d traffic item(s)", len(enabled))


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
            # frac is dot-separated groups: ms.us.ns — collapse to a fractional second.
            digits = frac.replace(".", "")
            base += int(digits) / (10 ** len(digits))
        return float(base)
    except (ValueError, AttributeError):
        return None


def _read_flow_stats(ixnetwork, vlan_base):
    """Per-ENI flow stats from the Flow Statistics view, keyed by global ENI index.

    Returns {eni_global: {"vlan": int, "tx": int, "rx": int, "loss_pct": float,
    "first_ts": float|None}}. ENI index = tracked VLAN id − vlan_base.
    """
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
    total_pages = data.TotalPages or 1
    for page in range(1, total_pages + 1):
        if data.CurrentPage != page:
            data.CurrentPage = page
        for raw in data.PageValues:
            cells = raw[0] if (raw and isinstance(raw[0], list)) else raw
            if ci_vlan is None:
                continue
            vlan = _i(cells[ci_vlan])
            if vlan <= 0:
                continue
            eni = vlan - vlan_base
            result[eni] = {
                "vlan": vlan,
                "tx": _i(cells[ci_tx]) if ci_tx is not None else 0,
                "rx": _i(cells[ci_rx]) if ci_rx is not None else 0,
                "loss_pct": _f(cells[ci_loss]) if ci_loss is not None else 0.0,
                "first_ts": _parse_ixn_timestamp(cells[ci_first]) if ci_first is not None else None,
            }
    return result


def _aggregate_loss_pct(stats):
    """Overall loss % across all flows from raw tx/rx totals."""
    tx = sum(s["tx"] for s in stats.values())
    rx = sum(s["rx"] for s in stats.values())
    if tx == 0:
        return 100.0
    return max(0.0, 100.0 * (tx - rx) / tx)


# ───────────────────────── results / correlation table ─────────────────────
def _print_traffic_vs_grpc(eni_indices, push_events, flow_stats):
    """Compare per-ENI gNMI push-complete deltas vs traffic first-seen deltas.

    eni_indices and flow_stats are keyed by global ENI index (= gNMI file index
    = VLAN − VLAN_OUT_BASE).
    """
    # gRPC "programmed" instant per ENI = when its map file finished pushing
    # (falls back to the eni file if no map file was pushed).
    grpc_ts = {}
    for ev in push_events.values():
        if ev["kind"] in ("map", "eni") and ev["idx"] is not None:
            cur = grpc_ts.get(ev["idx"])
            if cur is None or ev["kind"] == "map":
                grpc_ts[ev["idx"]] = ev["end"]

    rows = []
    for idx in eni_indices:
        g = grpc_ts.get(idx)
        st = flow_stats.get(idx, {})
        rows.append({
            "idx": idx,
            "grpc_ts": g,
            "traffic_ts": st.get("first_ts"),
            "rx": st.get("rx", 0),
            "loss": st.get("loss_pct", 100.0),
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

    # Global ENI indices we will program (one outbound flow / one VLAN each).
    # The rendered filename index IS the global ENI index, and the IxNetwork
    # outbound VLAN for it is VLAN_OUT_BASE + index.
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

    # ── IxNetwork: load known-good config, select target DPU's -Out TI ──────
    ti_name = TRAFFIC_ITEM_TEMPLATE.format(dpu=dpuhost.dpu_index)
    session, ixnetwork = _ix_connect()
    timings = {}
    mem_timeline = []
    push_events = {}
    flow_stats = {}
    total_start = time.time()
    traffic_started = False

    try:
        _ix_load_config(ixnetwork, IXNCFG_PATH)
        _ix_select_traffic_item(ixnetwork, ti_name)
        _ix_connect_ports(ixnetwork)
        _ix_generate_apply(ixnetwork)

        _ix_start_traffic(ixnetwork)
        traffic_started = True

        # Baseline: with no ENIs programmed we expect ~100% loss.
        logger.info("Baseline: letting traffic run %ds before programming ...", BASELINE_SETTLE_S)
        time.sleep(BASELINE_SETTLE_S)
        baseline_stats = _read_flow_stats(ixnetwork, VLAN_OUT_BASE)
        baseline_loss = _aggregate_loss_pct(baseline_stats)
        logger.info("Baseline aggregate loss: %.2f%% across %d flows (expected >= %.1f%%)",
                    baseline_loss, len(baseline_stats), BASELINE_MIN_LOSS_PCT)
        if baseline_loss < BASELINE_MIN_LOSS_PCT:
            logger.warning("Baseline loss %.2f%% below %.1f%% — some flows already forwarding "
                           "(stale config?). Continuing, but per-ENI deltas may be skewed.",
                           baseline_loss, BASELINE_MIN_LOSS_PCT)

        # ── Push all ENIs via gNMI while traffic runs ───────────────────────
        def _log_progress(filename, idx, kind, t0, t1):
            if kind == "map":  # map file is the last write that activates an ENI
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
    finally:
        if traffic_started:
            _ix_stop_traffic(ixnetwork)
        # Leave the loaded config in place; just drop our restpy session handle.
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

    # ── Correlation table: gNMI push vs. traffic bring-up ───────────────────
    n_forwarding = _print_traffic_vs_grpc(eni_indices, push_events, flow_stats)

    # ── Assertions ──────────────────────────────────────────────────────────
    final_loss = _aggregate_loss_pct(flow_stats)
    assert n_forwarding >= 1, (
        "No flow ever started forwarding — no ENI brought up traffic. "
        "Check IxNetwork wiring / chassis links / UHD encap / VLAN↔ENI mapping."
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
