import collections
import fnmatch
import importlib.util
import json
import logging
import os
import re
import shutil
import tempfile
import time

import pytest
from gnmi_utils import GNMIEnvironment

_RENDER_PATH = os.path.join(os.path.dirname(__file__), "configs", "dash_api_speed_pl", "render.py")
_render_spec = importlib.util.spec_from_file_location("dash_render", _RENDER_PATH)
render = importlib.util.module_from_spec(_render_spec)
_render_spec.loader.exec_module(render)

logger = logging.getLogger(__name__)

pytestmark = [
    pytest.mark.topology("smartswitch"),
    pytest.mark.skip_check_dut_health,
    pytest.mark.disable_loganalyzer,
    pytest.mark.sanity_check(skip_sanity=True),
]

# gnmi-agent container image
_GNMI_AGENT_IMAGE = "sonic-gnmi-agent:2026march13"


def _parse_mem_str(mem_str):
    """Parse a docker memory string like '512MiB', '1.5GiB', '256kB' into MiB."""
    m = re.match(r"([\d.]+)\s*(B|kB|MiB|GiB|TiB)", mem_str.strip())
    if not m:
        return 0.0
    val = float(m.group(1))
    unit = m.group(2)
    if unit == "B":
        return val / (1024 * 1024)
    if unit == "kB":
        return val / 1024
    if unit == "MiB":
        return val
    if unit == "GiB":
        return val * 1024
    if unit == "TiB":
        return val * 1024 * 1024
    return val


def _collect_memory(host):
    """Per-container memory (MiB) plus '_system_*' keys from `free -m`."""
    result = {}

    # awk avoids Jinja2 issues with Go's {{.Name}}; docker stats cols: ID NAME CPU% MEM_USED / ...
    out = host.shell(
        "docker stats --no-stream | awk 'NR>1 {print $2\"\\t\"$4}'",
        module_ignore_errors=True,
    )
    for line in out.get("stdout", "").splitlines():
        line = line.strip()
        if not line or "\t" not in line:
            continue
        name, used_str = line.split("\t", 1)
        result[name.strip()] = _parse_mem_str(used_str.strip())

    _parse_free_m(host, result)
    return result


def _parse_free_m(host, result):
    """Run ``free -m`` on *host* and populate *result* with system memory keys."""
    free_out = host.shell("free -m", module_ignore_errors=True)
    for line in free_out.get("stdout", "").splitlines():
        if line.startswith("Mem:"):
            parts = line.split()
            # free -m columns: total used free shared buff/cache available
            result["_system_total"] = float(parts[1])
            result["_system_used"] = float(parts[2])
            result["_system_free"] = float(parts[3])
            if len(parts) >= 7:
                result["_system_available"] = float(parts[6])


def _collect_free_memory(host):
    """Lightweight memory snapshot — only ``free -m``, no docker stats."""
    result = {}
    _parse_free_m(host, result)
    return result


def _collect_redis_memory(dpuhost):
    """Redis memory info from DPU_APPL_DB: totals plus 2 VNET_MAPPING key samples."""
    result = {}

    info_out = dpuhost.shell("sonic-db-cli DPU_APPL_DB INFO MEMORY",
                             module_ignore_errors=True)
    for line in info_out.get("stdout", "").splitlines():
        line = line.strip()
        if line.startswith("used_memory:"):
            try:
                result["_used_memory"] = int(line.split(":")[1])
            except ValueError:
                pass
        elif line.startswith("used_memory_human:"):
            result["_used_memory_human"] = line.split(":", 1)[1].strip()

    keys_out = dpuhost.shell(
        "sonic-db-cli DPU_APPL_DB KEYS 'DASH_VNET_MAPPING_TABLE:*' 2>/dev/null | head -2",
        module_ignore_errors=True,
    )
    for key in keys_out.get("stdout", "").splitlines():
        key = key.strip()
        if not key:
            continue
        usage_out = dpuhost.shell(
            f"sonic-db-cli DPU_APPL_DB MEMORY USAGE '{key}'",
            module_ignore_errors=True,
        )
        try:
            result[key] = int(usage_out.get("stdout", "0").strip())
        except ValueError:
            result[key] = 0

    return result


def _print_results(timings, total_elapsed, mem_before, mem_after,
                   redis_before, redis_after, mem_timeline=None):
    sep = "=" * 72
    logger.info(sep)
    logger.info("  DASH API LOAD SPEED TEST — RESULTS")
    logger.info(sep)

    logger.info("\n  Per-file load times:")
    logger.info("  %-44s  %8s", "File", "Time (s)")
    logger.info("  " + "-" * 56)
    for filename, elapsed in timings.items():
        logger.info("  %-44s  %8.2f", filename, elapsed)
    logger.info("  " + "-" * 56)
    logger.info("  %-44s  %8.2f", "TOTAL", total_elapsed)
    logger.info("  %-44s  %8.2f", "Average per file", total_elapsed / len(timings))
    logger.info("  Files loaded: %d", len(timings))

    for host_label in ("NPU", "DPU"):
        before = mem_before[host_label]
        after = mem_after[host_label]

        all_containers = sorted(
            k for k in set(before) | set(after) if not k.startswith("_")
        )

        logger.info("\n  Memory usage — %s (MiB):", host_label)
        logger.info("  %-30s  %8s  %8s  %8s", "Container", "Before", "After", "Delta")
        logger.info("  " + "-" * 58)

        total_before = 0.0
        total_after = 0.0
        for name in all_containers:
            b = before.get(name, 0.0)
            a = after.get(name, 0.0)
            total_before += b
            total_after += a
            logger.info("  %-30s  %8.1f  %8.1f  %+8.1f", name, b, a, a - b)

        logger.info("  " + "-" * 58)
        logger.info(
            "  %-30s  %8.1f  %8.1f  %+8.1f",
            "Containers total",
            total_before,
            total_after,
            total_after - total_before,
        )

        sys_b = before.get("_system_used", 0.0)
        sys_a = after.get("_system_used", 0.0)
        logger.info(
            "  %-30s  %8.1f  %8.1f  %+8.1f",
            "System used (free -m)",
            sys_b,
            sys_a,
            sys_a - sys_b,
        )
        sys_total = before.get("_system_total", after.get("_system_total", 0.0))
        for key, label in [("_system_free", "System free"),
                           ("_system_available", "System available")]:
            b = before.get(key, 0.0)
            a = after.get(key, 0.0)
            logger.info(
                "  %-30s  %8.1f  %8.1f  %+8.1f",
                label, b, a, a - b,
            )
        if sys_total:
            logger.info("  %-30s  %8.1f", "System total", sys_total)

    # Memory timeline (per-file free memory after each push)
    if mem_timeline:
        logger.info("\n  Memory timeline — free memory after each file push (MiB):")
        logger.info(
            "  %-6s  %-40s  %7s  %9s  %9s  %9s  %9s",
            "#", "File", "Ops",
            "NPU free", "NPU avail", "DPU free", "DPU avail")
        logger.info("  " + "-" * 96)
        for entry in mem_timeline:
            logger.info(
                "  %-6s  %-40s  %7d  %9.0f  %9.0f  %9.0f  %9.0f",
                entry["idx"], entry["file"][:40], entry["ops"],
                entry["npu_free"], entry["npu_available"],
                entry["dpu_free"], entry["dpu_available"])
        # Summary: min free across all snapshots
        if len(mem_timeline) > 1:
            logger.info("  " + "-" * 96)
            logger.info(
                "  %-6s  %-40s  %7s  %9.0f  %9.0f  %9.0f  %9.0f",
                "", "MINIMUM", "",
                min(e["npu_free"] for e in mem_timeline),
                min(e["npu_available"] for e in mem_timeline),
                min(e["dpu_free"] for e in mem_timeline),
                min(e["dpu_available"] for e in mem_timeline))

    # Redis (DPU_APPL_DB) memory
    logger.info("\n  DPU Redis memory — DPU_APPL_DB (bytes):")
    logger.info("  %-52s  %10s  %10s  %10s", "Key", "Before", "After", "Delta")
    logger.info("  " + "-" * 86)

    redis_b_total = redis_before.get("_used_memory", 0)
    redis_a_total = redis_after.get("_used_memory", 0)
    logger.info(
        "  %-52s  %10d  %10d  %+10d",
        "used_memory (total)",
        redis_b_total,
        redis_a_total,
        redis_a_total - redis_b_total,
    )
    logger.info(
        "  %-52s  %10s  %10s",
        "used_memory_human",
        redis_before.get("_used_memory_human", "n/a"),
        redis_after.get("_used_memory_human", "n/a"),
    )

    sample_keys = sorted(k for k in set(redis_before) | set(redis_after) if not k.startswith("_"))
    for key in sample_keys:
        b = redis_before.get(key, 0)
        a = redis_after.get(key, 0)
        logger.info("  %-52s  %10d  %10d  %+10d", key, b, a, a - b)

    logger.info(sep)


# ── Regex for parsing the TIMING BREAKDOWN block from gnmi_client.py output ──
_PHASE_LINE_RE = re.compile(r"^\s+(\S+)\s+([\d.]+)\s+s\s*$")

# Phases in display order (matches _log_timing_summary in go_gnmi_utils.py)
_PHASE_ORDER = [
    "json_load", "template_render", "proto_serialize",
    "proto_file_write", "cmd_build", "gnmi_set_subprocess",
    "proto_cleanup", "pipeline_wait", "sleep",
]


def _parse_timing_breakdown(output_text):
    """Extract {phase_name: seconds} from a TIMING BREAKDOWN block; empty if not found."""
    phases = {}
    in_block = False
    for line in output_text.splitlines():
        stripped = line.strip()
        if "TIMING BREAKDOWN" in stripped:
            in_block = True
            continue
        if in_block:
            if "TOTAL accounted" in stripped:
                m = _PHASE_LINE_RE.match(line)
                if m:
                    phases["TOTAL_accounted"] = float(m.group(2))
                break
            m = _PHASE_LINE_RE.match(line)
            if m:
                phases[m.group(1)] = float(m.group(2))
    return phases


def _print_gnmi_timing_breakdown(timings, sub_timings):
    """Print per-file sub-op timings. timings={file: wall_s}; sub_timings={file: {phase: s}}."""
    if not sub_timings:
        return

    # Human-friendly labels for each phase
    _PHASE_LABELS = {
        "json_load":          "JSON Load",
        "template_render":    "Template Render",
        "proto_serialize":    "Proto Serialize",
        "proto_file_write":   "File Write",
        "cmd_build":          "Cmd Build",
        "gnmi_set_subprocess": "gNMI Set (RPC)",
        "proto_cleanup":      "Cleanup",
        "pipeline_wait":      "Pipeline Wait",
        "sleep":              "Sleep",
    }

    # Collect all phases that appeared in any file, in display order
    all_phases = []
    for phase in _PHASE_ORDER:
        if any(phase in st for st in sub_timings.values()):
            all_phases.append(phase)
    # Add any unexpected phases not in _PHASE_ORDER
    extra = sorted(
        set(p for st in sub_timings.values() for p in st)
        - set(_PHASE_ORDER) - {"TOTAL_accounted"}
    )
    all_phases.extend(extra)

    col_w = 16   # width of each phase column
    file_w = 44  # width of the file name column
    wall_w = 12  # width of the Wall Clock column
    acct_w = 12  # width of the Accounted column
    unacc_w = 12  # width of the Unaccounted column
    row_w = file_w + wall_w + col_w * len(all_phases) + acct_w + unacc_w

    sep = "=" * row_w
    thin_sep = "-" * row_w

    logger.info("")
    logger.info(sep)
    logger.info("  GNMI SUB-OPERATION TIMING BREAKDOWN")
    logger.info("  All times in seconds.  'Wall Clock' = total elapsed real time per docker exec call.")
    logger.info("  'Accounted' = sum of instrumented phases.  'Unaccounted' = Wall Clock - Accounted")
    logger.info("  (process startup, gRPC connect, stdout flush, Python interpreter overhead, etc.)")
    logger.info(sep)

    # Header row
    phase_hdrs = "".join(
        ("{:>%d}" % col_w).format(_PHASE_LABELS.get(p, p)[:col_w])
        for p in all_phases
    )
    hdr = ("  {:<{fw}}{:>{ww}}{phases}{:>{aw}}{:>{uw}}").format(
        "File", "Wall Clock", "Accounted", "Unaccounted",
        fw=file_w, ww=wall_w, aw=acct_w, uw=unacc_w, phases=phase_hdrs,
    )
    logger.info(hdr)
    logger.info("  " + thin_sep)

    # Accumulators for the totals row
    totals = collections.defaultdict(float)
    total_wall = 0.0
    total_accounted = 0.0

    for filename in sorted(timings.keys()):
        wall = timings[filename]
        total_wall += wall
        st = sub_timings.get(filename, {})
        accounted = st.get("TOTAL_accounted", sum(st.get(p, 0.0) for p in all_phases))
        total_accounted += accounted
        unaccounted = wall - accounted

        vals = ""
        for p in all_phases:
            v = st.get(p, 0.0)
            totals[p] += v
            if v > 0:
                vals += ("{:>%d.3f}" % col_w).format(v)
            else:
                vals += ("{:>%d}" % col_w).format("-")

        logger.info(
            "  {:<{fw}}{:>{ww}.2f}{vals}{:>{aw}.3f}{:>{uw}.3f}".format(
                filename[:file_w], wall, accounted, unaccounted,
                fw=file_w, ww=wall_w, aw=acct_w, uw=unacc_w, vals=vals,
            )
        )

    # Totals row
    logger.info("  " + thin_sep)
    tot_vals = "".join(("{:>%d.3f}" % col_w).format(totals[p]) for p in all_phases)
    total_unaccounted = total_wall - total_accounted
    logger.info(
        "  {:<{fw}}{:>{ww}.2f}{vals}{:>{aw}.3f}{:>{uw}.3f}".format(
            "TOTAL", total_wall, total_accounted, total_unaccounted,
            fw=file_w, ww=wall_w, aw=acct_w, uw=unacc_w, vals=tot_vals,
        )
    )

    # Average row
    if timings:
        n = len(timings)
        avg_vals = "".join(("{:>%d.3f}" % col_w).format(totals[p] / n) for p in all_phases)
        logger.info(
            "  {:<{fw}}{:>{ww}.2f}{vals}{:>{aw}.3f}{:>{uw}.3f}".format(
                "AVERAGE", total_wall / n, total_accounted / n, total_unaccounted / n,
                fw=file_w, ww=wall_w, aw=acct_w, uw=unacc_w, vals=avg_vals,
            )
        )

    # Percentage-of-wall-time row
    if total_wall > 0:
        pct_vals = ""
        for p in all_phases:
            pct = 100.0 * totals[p] / total_wall
            pct_vals += ("{:>%d}" % col_w).format("%.1f%%" % pct)
        acct_pct = "%.1f%%" % (100.0 * total_accounted / total_wall)
        unacc_pct = "%.1f%%" % (100.0 * total_unaccounted / total_wall)
        logger.info("  " + thin_sep)
        logger.info(
            "  {:<{fw}}{:>{ww}}{vals}{:>{aw}}{:>{uw}}".format(
                "% of Wall Clock", "100.0%", acct_pct, unacc_pct,
                fw=file_w, ww=wall_w, aw=acct_w, uw=unacc_w, vals=pct_vals,
            )
        )

    logger.info(sep)
    logger.info("")


_NPU_STATIC_ARP = [
    ("220.0.1.2", "80:09:02:02:00:01"),
    ("220.0.2.2", "80:09:02:02:00:02"),
    ("220.0.3.2", "80:09:02:02:00:03"),
    ("220.0.4.2", "80:09:02:02:00:04"),
]


def npu_pre_config(duthost, dpu_midplane_ip, dpu_dataplane_ip):
    """Prepare NPU for DASH push: static ARPs, midplane/dataplane ping, log routes/ifaces."""
    logger.info("NPU: adding permanent static ARP entries for dataplane next-hops")
    for ip, mac in _NPU_STATIC_ARP:
        route_out = duthost.shell(f"ip route get {ip}", module_ignore_errors=True)
        dev = None
        for token in route_out.get("stdout", "").split():
            if token == "dev":
                idx = route_out.get("stdout", "").split().index("dev")
                dev = route_out.get("stdout", "").split()[idx + 1]
                break
        assert dev, f"Could not determine egress interface for {ip} on NPU"

        for attempt in range(3):
            duthost.shell(
                f"sudo ip neigh replace {ip} lladdr {mac} dev {dev} nud permanent",
                module_ignore_errors=True,
            )
            verify = duthost.shell(f"ip neigh show {ip}", module_ignore_errors=True)
            if "PERMANENT" in verify.get("stdout", "").upper():
                logger.info("  NPU: permanent ARP %s lladdr %s dev %s (attempt %d)",
                            ip, mac, dev, attempt + 1)
                break
        else:
            raise AssertionError(
                f"Failed to add permanent ARP entry for {ip} after 3 attempts. "
                f"'ip neigh show {ip}': {verify.get('stdout', '')}"
            )

    logger.info("NPU: pinging DPU midplane IP %s to populate ARP", dpu_midplane_ip)
    duthost.shell(f"ping -c 3 -W 2 {dpu_midplane_ip}", module_ignore_errors=True)

    arp_out = duthost.shell(f"ip n show {dpu_midplane_ip}", module_ignore_errors=True)
    logger.info("NPU ARP entry for %s: %s", dpu_midplane_ip,
                arp_out.get("stdout", "").strip() or "(none)")

    logger.info("NPU: show ip route")
    npu_route = duthost.shell("show ip route", module_ignore_errors=True)
    for line in npu_route.get("stdout", "").splitlines():
        logger.info("  NPU route: %s", line)

    logger.info("NPU: show ip interfaces")
    npu_ifaces = duthost.shell("show ip interfaces", module_ignore_errors=True)
    for line in npu_ifaces.get("stdout", "").splitlines():
        logger.info("  NPU iface: %s", line)

    logger.info("NPU: pinging DPU dataplane IP %s", dpu_dataplane_ip)
    ping_out = duthost.shell(f"ping -c 5 -W 2 {dpu_dataplane_ip}", module_ignore_errors=True)
    for line in ping_out.get("stdout", "").splitlines():
        logger.info("  %s", line)


def dpu_pre_config(dpuhost):
    """Prepare DPU for DASH push: add+verify Loopback0 IP, log routes/ifaces."""
    loopback_ip = "221.0.0.%d/32" % (dpuhost.dpu_index + 1)
    logger.info("DPU: creating Loopback0 interface (if not present)")
    dpuhost.shell("sudo config loopback add Loopback0", module_ignore_errors=True)
    logger.info("DPU: adding Loopback0 IP %s", loopback_ip)
    dpuhost.shell("sudo config interface ip add Loopback0 %s" % loopback_ip, module_ignore_errors=True)
    iface_out = dpuhost.shell("show ip interfaces")
    assert "221.0.0.%d" % (dpuhost.dpu_index + 1) in iface_out.get("stdout", ""), \
        "Loopback0 IP %s was not found in 'show ip interfaces' after config" % loopback_ip

    logger.info("DPU: show ip route")
    dpu_route = dpuhost.shell("show ip route", module_ignore_errors=True)
    for line in dpu_route.get("stdout", "").splitlines():
        logger.info("  DPU route: %s", line)

    logger.info("DPU: show ip interfaces")
    dpu_ifaces = dpuhost.shell("show ip interfaces", module_ignore_errors=True)
    for line in dpu_ifaces.get("stdout", "").splitlines():
        logger.info("  DPU iface: %s", line)


def _count_json_operations(filepath):
    """Count SET/DEL operations and distinct table types in a config JSON file."""
    with open(filepath) as f:
        operations = json.load(f)
    tables = {}
    for op in operations:
        op_type = op.get("OP", "?")
        for k in op:
            if k == "OP":
                continue
            table = k.split(":")[0]
            tables.setdefault(table, {"SET": 0, "DEL": 0})
            tables[table][op_type] = tables[table].get(op_type, 0) + 1
    return len(operations), tables


def _verify_dpu_appl_db(dpuhost, table_pattern, label=""):
    """Query DPU_APPL_DB for keys matching table_pattern and return count + sample keys."""
    quiet = "DASH_VNET_MAPPING_TABLE" in table_pattern
    out = dpuhost.shell(
        f"sonic-db-cli DPU_APPL_DB KEYS '{table_pattern}' 2>/dev/null",
        module_ignore_errors=True,
        verbose=not quiet,
    )
    keys = [k.strip() for k in out.get("stdout", "").splitlines() if k.strip()]
    if label:
        logger.info("  DPU_APPL_DB %s: %d keys matching '%s'", label, len(keys), table_pattern)
        for k in keys[:5]:
            logger.info("    sample: %s", k)
        if len(keys) > 5:
            logger.info("    ... and %d more", len(keys) - 5)
    return keys


def _container_path_to_host(container_path):
    """Collapse a repeated adjacent dir (e.g. .../x/x/... → .../x/...) for Docker-in-Docker paths."""
    parts = container_path.split("/")
    for i in range(1, len(parts) - 1):
        if parts[i] and parts[i] == parts[i + 1]:
            candidate = "/".join(parts[:i] + parts[i + 1:])
            logger.info("Path translation: %s -> %s (collapsed '%s')",
                        container_path, candidate, parts[i])
            return candidate
    return container_path


_GNMI_CONTAINER_NAME = "sonic-gnmi-agent-push"

# Throttle large pushes so DPU ZMQ/orchagent can drain (prior incident: 64k entries OOM-crashed orchagent).
_THROTTLE_OP_THRESHOLD = 5000     # files with fewer ops are not throttled
_THROTTLE_SEC_PER_1K_OPS = 0.5   # 0.5 s per 1 000 ops  →  32 s for 64 000 ops


def load_json_via_gnmi(localhost, duthost, dpuhost, config_dir, files, timings,
                       sub_timings=None, mem_timeline=None):
    """Push each JSON via a long-lived sonic-gnmi-agent container (config_dir mounted at /dpu)."""
    if sub_timings is None:
        sub_timings = {}
    if mem_timeline is None:
        mem_timeline = []
    env = GNMIEnvironment(duthost)
    dpu_index = dpuhost.dpu_index
    ip = duthost.mgmt_ip
    port = env.gnmi_port

    # Translate container path → host path for docker bind mount.
    host_config_dir = _container_path_to_host(config_dir)
    logger.info("config_dir (container): %s", config_dir)
    logger.info("config_dir (host):      %s", host_config_dir)

    # Snapshot DPU_APPL_DB key count before pushing
    db_before = dpuhost.shell(
        "sonic-db-cli DPU_APPL_DB DBSIZE",
        module_ignore_errors=True,
    )
    logger.info("DPU_APPL_DB DBSIZE before push: %s", db_before.get("stdout", "").strip())

    # Start a persistent container (reuse if already running).
    localhost.shell(
        f"docker rm -f {_GNMI_CONTAINER_NAME}",
        module_ignore_errors=True,
    )
    start_out = localhost.shell(
        f"docker run -d --name {_GNMI_CONTAINER_NAME} --network host"
        f" --shm-size=256m"
        f" --mount src={host_config_dir},target=/dpu,type=bind,readonly"  # noqa: E231
        f" {_GNMI_AGENT_IMAGE} -c 'sleep infinity'",
        module_ignore_errors=True,
    )
    if start_out.get("rc", 1) != 0:
        pytest.fail("Could not start %s: %s" % (
            _GNMI_CONTAINER_NAME, start_out.get("stderr", "")))
    logger.info("Started persistent container %s", _GNMI_CONTAINER_NAME)

    # Pre-count operations for all files (outside the timed loop).
    file_info = []
    all_tables = set()
    for filename in files:
        local_path = os.path.join(config_dir, filename)
        op_count, tables = _count_json_operations(local_path)
        file_info.append((filename, op_count, tables))
        all_tables.update(tables.keys())

    # ── Pre-check: verify gNMI server is reachable before pushing files ──
    logger.info("Pre-check: verifying gNMI connectivity to %s:%s ...", ip, port)
    check_cmd = (
        f"docker exec {_GNMI_CONTAINER_NAME}"
        f" /usr/sbin/gnmi_set -insecure -target_addr {ip}:{port}"  # noqa: E231
        f" -username admin -password password"
    )
    check_out = localhost.shell(check_cmd, module_ignore_errors=True)
    check_rc = check_out.get("rc", -1)
    check_stderr = check_out.get("stderr", "")
    if check_rc != 0 and ("DeadlineExceeded" in check_stderr
                          or "connection refused" in check_stderr.lower()
                          or "unavailable" in check_stderr.lower()
                          or "transport" in check_stderr.lower()):
        pytest.fail(
            f"gNMI server unreachable at {ip}:{port} — aborting.\n"  # noqa: E231
            f"stderr: {check_stderr[:500]}"
        )
    logger.info("Pre-check: gNMI server reachable (rc=%d)", check_rc)

    push_errors = []

    for idx, (filename, op_count, tables) in enumerate(file_info, start=1):
        table_summary = ", ".join(
            "{0}:{1}S/{2}D".format(t, tables[t]['SET'], tables[t]['DEL'])
            for t in sorted(tables)
        )
        logger.info("  [%d/%d] pushing %s (%d ops: %s) ...",
                    idx, len(files), filename, op_count, table_summary)

        # Large files use smaller batch to avoid saturating the DPU's ZMQ queue.
        batch_val = 1000 if op_count >= _THROTTLE_OP_THRESHOLD else 10000
        cmd = (
            f"docker exec {_GNMI_CONTAINER_NAME}"
            f" gnmi_client.py --batch_val {batch_val} --no-proto -i {dpu_index}"
            f" -n 8 -t {ip}:{port} update -f /dpu/{filename}"  # noqa: E231
        )

        t_start = time.time()
        out = localhost.shell(cmd, module_ignore_errors=True)
        elapsed = time.time() - t_start
        timings[filename] = elapsed

        rc = out.get("rc", -1)
        stdout = out.get("stdout", "")
        stderr = out.get("stderr", "")

        # Extract and log TIMING/breakdown lines from gnmi_client.py instrumentation.
        combined_output = stdout + "\n" + stderr
        for line in combined_output.splitlines():
            stripped = line.strip()
            if "TIMING" in stripped or "=====" in stripped or "-----" in stripped \
                    or "TOTAL accounted" in stripped or stripped.startswith("json_load") \
                    or stripped.startswith("proto_") or stripped.startswith("cmd_build") \
                    or stripped.startswith("gnmi_set_") or stripped.startswith("pipeline_") \
                    or stripped.startswith("sleep"):
                logger.info("  [%d/%d] %s", idx, len(files), stripped)

        # Parse the TIMING BREAKDOWN block into sub_timings for the summary table.
        phases = _parse_timing_breakdown(combined_output)
        if phases:
            sub_timings[filename] = phases

        # Only log errors and summary — skip per-line stderr for speed.
        failed = False
        failure_reason = ""

        if rc != 0:
            failed = True
            failure_reason = f"exit code {rc}"
        elif "Set failed" in stderr or "GRPC error" in stderr or "Error" in stderr:
            failed = True
            failure_reason = "error string in output"

        if failed:
            logger.error("  [%d/%d] FAILED %s after %.2fs — %s\n  stderr (tail): %s",
                         idx, len(files), filename, elapsed, failure_reason,
                         stderr[-3000:])
            push_errors.append(f"{filename}: {failure_reason}")
            # Fail fast — if the first file fails, no point continuing
            if idx == 1:
                logger.error("First file failed — aborting remaining files")
                break
        else:
            logger.info("  [%d/%d] done    %-40s  %.2fs  rc=%d",
                        idx, len(files), filename, elapsed, rc)

        # ── Throttle after large pushes to let the DPU drain its ZMQ queue ──
        if not failed and op_count >= _THROTTLE_OP_THRESHOLD:
            throttle_secs = round(_THROTTLE_SEC_PER_1K_OPS * op_count / 1000, 1)
            logger.info("  [%d/%d] throttle %.1fs  (%d ops > %d threshold)",
                        idx, len(files), throttle_secs, op_count,
                        _THROTTLE_OP_THRESHOLD)
            time.sleep(throttle_secs)

        # ── Per-file memory snapshot (lightweight — free -m only) ──
        try:
            npu_mem = _collect_free_memory(duthost)
            dpu_mem = _collect_free_memory(dpuhost)
            mem_timeline.append({
                "idx": idx,
                "file": filename,
                "ops": op_count,
                "npu_free": npu_mem.get("_system_free", 0),
                "npu_available": npu_mem.get("_system_available", 0),
                "dpu_free": dpu_mem.get("_system_free", 0),
                "dpu_available": dpu_mem.get("_system_available", 0),
            })
            logger.info(
                "  [%d/%d] mem: NPU free=%dM avail=%dM | DPU free=%dM avail=%dM",
                idx, len(files),
                npu_mem.get("_system_free", 0), npu_mem.get("_system_available", 0),
                dpu_mem.get("_system_free", 0), dpu_mem.get("_system_available", 0),
            )
        except Exception:
            logger.debug("  [%d/%d] mem snapshot failed (non-fatal)", idx, len(files))

    # Stop the persistent container.
    localhost.shell(
        f"docker rm -f {_GNMI_CONTAINER_NAME}",
        module_ignore_errors=True,
    )

    # Batch verification: check DPU_APPL_DB once for all tables.
    for table in sorted(all_tables):
        _verify_dpu_appl_db(dpuhost, "%s:*" % table, label="after all files")

    # Final DB size check
    db_after = dpuhost.shell(
        "sonic-db-cli DPU_APPL_DB DBSIZE",
        module_ignore_errors=True,
    )
    logger.info("DPU_APPL_DB DBSIZE after push: %s (was: %s)",
                db_after.get("stdout", "").strip(),
                db_before.get("stdout", "").strip())

    if push_errors:
        pytest.fail("gNMI push had %d error(s):\n%s" % (
            len(push_errors), "\n".join("  - %s" % e for e in push_errors)))


def test_dash_api_load_speed_pl(localhost, duthost, dpuhosts, dpu_index):
    """Render DASH configs to a temp dir then push via gnmi_client.py; record per-file load time."""
    dpuhost = dpuhosts[dpu_index]

    # Pre-flight: SSH port check (ping/midplane-status unreliable after route removal).
    dpu_name = f"DPU{dpuhost.dpu_index}"
    dpu_midplane_ip = "169.254.200.%d" % (dpuhost.dpu_index + 1)
    logger.info("Pre-flight: assuming %s is up at %s (no automated check)", dpu_name, dpu_midplane_ip)

    # Generate DASH config JSONs on the fly via the Jinja2 renderer.
    render_output_dir = tempfile.mkdtemp(prefix="dash_cfg_")
    logger.info("Rendering DASH configs into %s", render_output_dir)
    render.generate(dict(render.DEFAULTS), render_output_dir, prefix="pl_100")

    config_dir = os.path.join(render_output_dir, f"dpu{dpuhost.dpu_index}")
    assert os.path.isdir(config_dir), \
        f"Config directory not found after render: {config_dir}"

    pattern = f"*dpu{dpuhost.dpu_index}*.json"
    files = sorted(
        f for f in os.listdir(config_dir)
        if fnmatch.fnmatch(f, pattern) and f.endswith(".json")
    )
    assert files, f"No JSON config files found matching '{pattern}' in {config_dir}"
    logger.info(
        "Rendered %d config files to load for dpu%d",
        len(files), dpuhost.dpu_index,
    )

    # ── Derive DPU IPs based on hwsku ──────────────────────────────────────
    hwsku = duthost.facts.get("hwsku", "")
    logger.info("NPU hwsku: %s", hwsku)

    dpu_midplane_ip = "169.254.200.%d" % (dpuhost.dpu_index + 1)
    if "Cisco" in hwsku:
        dpu_dataplane_ip = "18.%d.202.1" % dpuhost.dpu_index
    else:
        dpu_dataplane_ip = "10.0.0.%d" % (57 + dpuhost.dpu_index * 2)
    logger.info("DPU%d dataplane IP: %s", dpuhost.dpu_index, dpu_dataplane_ip)

    mem_before = {
        "NPU": _collect_memory(duthost),
        "DPU": _collect_memory(dpuhost),
    }
    redis_before = _collect_redis_memory(dpuhost)

    dpu_pre_config(dpuhost)
    npu_pre_config(duthost, dpu_midplane_ip, dpu_dataplane_ip)

    timings = {}
    sub_timings = {}
    mem_timeline = []
    total_start = time.time()

    try:
        load_json_via_gnmi(localhost, duthost, dpuhost,
                           config_dir, files, timings,
                           sub_timings, mem_timeline)
    finally:
        shutil.rmtree(render_output_dir, ignore_errors=True)
        logger.info("Cleaned up rendered config dir: %s", render_output_dir)

        # Always print results, even if the load raised an exception.
        total_elapsed = time.time() - total_start
        try:
            mem_after = {
                "NPU": _collect_memory(duthost),
                "DPU": _collect_memory(dpuhost),
            }
            redis_after = _collect_redis_memory(dpuhost)
            _print_results(timings, total_elapsed, mem_before, mem_after,
                           redis_before, redis_after, mem_timeline)
            if sub_timings:
                _print_gnmi_timing_breakdown(timings, sub_timings)
        except Exception:
            logger.exception("Failed to collect/print post-test results")

    # Check DPU alive via dataplane ping (midplane reachability unreliable after route removal).
    midplane_out = duthost.show_and_parse("show chassis module midplane-status")
    dpu_row = next((r for r in midplane_out if r.get("name", "").strip().upper() == dpu_name), None)
    midplane_reachability = dpu_row.get("reachability", "").strip() if dpu_row else "unknown"
    logger.info("%s midplane reachability after push: %s (expected False — midplane route removed)",
                dpu_name, midplane_reachability)

    logger.info("Verifying %s is alive via dataplane ping to %s ...", dpu_name, dpu_dataplane_ip)
    ping_out = duthost.shell(f"ping -c 3 -W 2 {dpu_dataplane_ip}", module_ignore_errors=True)
    ping_ok = ping_out.get("rc", 1) == 0
    for line in ping_out.get("stdout", "").splitlines():
        logger.info("  %s", line)
    assert ping_ok, (
        f"{dpu_name} is unreachable via dataplane IP {dpu_dataplane_ip} after push. "
        "DPU may have crashed — check DPU logs."
    )
    logger.info("%s dataplane reachability after push: OK", dpu_name)

    # Verify ENIs propagated from APPL_DB into COUNTERS_ENI_NAME_MAP (takes time).
    _ENI_EXPECTED = (len(files) - 1) // 2  # 2 files per ENI (eni, map) + apl per DPU
    if _ENI_EXPECTED < 1:
        _ENI_EXPECTED = 1
    _ENI_POLL_INTERVAL = 4   # seconds between polls
    _ENI_TIMEOUT = 15        # 15 seconds total
    logger.info("DPU: waiting for %d ENIs in COUNTERS_ENI_NAME_MAP (timeout %ds)...",
                _ENI_EXPECTED, _ENI_TIMEOUT)
    deadline = time.time() + _ENI_TIMEOUT
    eni_count = 0
    while time.time() < deadline:
        eni_out = dpuhost.shell(
            'sonic-db-cli COUNTERS_DB HGETALL "COUNTERS_ENI_NAME_MAP"',
            module_ignore_errors=True,
        )
        eni_stdout = eni_out.get("stdout", "")
        # sonic-db-cli returns a Python-repr dict; count keys via 'eni-' occurrences.
        eni_count = eni_stdout.count("eni-")
        logger.info("DPU: ENIs found: %d / %d", eni_count, _ENI_EXPECTED)
        logger.info("DPU: COUNTERS_ENI_NAME_MAP raw output:\n%s", eni_stdout or "(empty)")
        if eni_count >= _ENI_EXPECTED:
            break
        time.sleep(_ENI_POLL_INTERVAL)
    assert eni_count >= _ENI_EXPECTED, \
        "Expected %d ENIs in COUNTERS_ENI_NAME_MAP but found %d after %ds" % (
            _ENI_EXPECTED, eni_count, _ENI_TIMEOUT)
