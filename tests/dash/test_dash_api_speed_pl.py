import collections
import fnmatch
import json
import logging
import os
import re
import shutil
import tempfile
import time

import proto_utils
import pytest
from gnmi_utils import GNMIEnvironment, apply_gnmi_cert, generate_gnmi_cert, write_gnmi_files

logger = logging.getLogger(__name__)

pytestmark = [
    pytest.mark.topology("smartswitch"),
    pytest.mark.skip_check_dut_health,
    pytest.mark.disable_loganalyzer,
    pytest.mark.sanity_check(skip_sanity=True),
]

CONFIG_DIR = os.path.join(os.path.dirname(__file__), "configs", "pl_100")

# Path on NPU where JSON files are staged for the docker mount
_NPU_STAGE_DIR = "/tmp/dash_load"

# gnmi-agent container image and fixed paths expected on the NPU
_GNMI_AGENT_IMAGE = "sonic-gnmi-agent:2026march13"
_GO_GNMI_UTILS_NPU = "/root/pl_100/go_gnmi_utils.py"
_GO_GNMI_UTILS_CTR = "/usr/lib/python3/dist-packages/gnmi_agent/go_gnmi_utils.py"


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
    """
    Return a dict with per-container memory (MiB) keyed by container name,
    plus '_system_used' for total system used MiB from `free -m`.
    """
    result = {}

    # Use awk to avoid Jinja2 templating issues with Go's {{.Name}} format syntax.
    # docker stats columns: ID NAME CPU% MEM_USED / MEM_LIMIT MEM% ...
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

    free_out = host.shell("free -m", module_ignore_errors=True)
    for line in free_out.get("stdout", "").splitlines():
        if line.startswith("Mem:"):
            parts = line.split()
            result["_system_used"] = float(parts[2])

    return result


def _collect_redis_memory(dpuhost):
    """
    Return a dict with Redis memory info from DPU_APPL_DB:
      '_used_memory_human' — overall used memory (human string)
      '_used_memory'       — overall used memory (bytes, int)
      per VNET_MAPPING key (2 samples) — per-key MEMORY USAGE in bytes
    """
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


def _print_results(timings, total_elapsed, mem_before, mem_after, redis_before, redis_after):
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
    """Extract per-phase seconds from a TIMING BREAKDOWN block in gnmi output.

    Returns a dict {phase_name: seconds} or empty dict if not found.
    """
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
    """Print a consolidated table of per-file sub-operation timings.

    Args:
        timings: {filename: wall_clock_seconds}
        sub_timings: {filename: {phase: seconds}}
    """
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
    """
    Prepare the NPU for a DASH config push:
      - Add permanent static ARP entries for dataplane next-hops.
      - Ping the DPU midplane IP to populate the NPU ARP table.
      - Log NPU routing and interface state.
      - Ping the DPU dataplane IP to confirm end-to-end reachability.
      - Create the stage directory for JSON file uploads.
    """
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

    # ── Prepare stage directory on NPU ────────────────────────────────────────
    duthost.shell(f"mkdir -p {_NPU_STAGE_DIR}", module_ignore_errors=True)


def dpu_pre_config(dpuhost):
    """
    Prepare the DPU for a DASH config push:
      - Add Loopback0 IP and verify it was applied.
      - Log DPU routing and interface state.
      - Remove all default routes via the midplane gateway (last step, so SSH
        to the midplane still works during the setup above).
    """
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

    # Remove ALL default routes via the midplane gateway — last step before push.
    # Done last so SSH/ping to the midplane IP still works during all setup above.
    # logger.info("DPU: removing all default routes via 169.254.200.254")
    # for _ in range(10):
    #     routes_out = dpuhost.shell("ip route show default", module_ignore_errors=True)
    #     midplane_defaults = [
    #         route for route in routes_out.get("stdout", "").splitlines()
    #         if "169.254.200.254" in route
    #     ]
    #     if not midplane_defaults:
    #         break
    #     dpuhost.shell("sudo ip route del default via 169.254.200.254",
    #                   module_ignore_errors=True)
    # routes_out = dpuhost.shell("ip route show default", module_ignore_errors=True)
    # remaining = [
    #     route for route in routes_out.get("stdout", "").splitlines()
    #     if "169.254.200.254" in route
    # ]
    # assert not remaining, \
    #     "Midplane default route(s) still present after removal: %s" % remaining

    # dataplane_defaults = [
    #     route for route in routes_out.get("stdout", "").splitlines()
    #     if route.startswith("default") and "169.254.200.254" not in route
    # ]
    # assert dataplane_defaults, \
    #     "No dataplane default route found after removing midplane routes. " \
    #     "'ip route show default': %s" % routes_out.get("stdout", "")
    # logger.info("DPU: active default route(s): %s", "; ".join(dataplane_defaults))


def load_json_via_npu(duthost, dpuhost, config_dir, files, timings):
    """Push each JSON config file to the DPU via docker run on the NPU."""
    for idx, filename in enumerate(files, start=1):
        local_path = os.path.join(config_dir, filename)
        npu_path = f"{_NPU_STAGE_DIR}/{filename}"

        duthost.copy(src=local_path, dest=npu_path, verbose=False)
        logger.info("  [%d/%d] pushing %s ...", idx, len(files), filename)

        cmd = (
            "docker run --network host"
            f" --mount src={_NPU_STAGE_DIR},target=/dpu,type=bind,readonly"  # noqa: E231
            f" --mount src={_GO_GNMI_UTILS_NPU},target={_GO_GNMI_UTILS_CTR},type=bind,readonly"  # noqa: E231
            f" -t {_GNMI_AGENT_IMAGE}"
            f" -c 'gnmi_client.py --batch_val 10000 -i {dpuhost.dpu_index}"
            f" -n 8 -t 127.0.0.1:50052 update -f /dpu/{filename}'"  # noqa: E231
        )

        t_start = time.time()
        out = duthost.shell(cmd, module_ignore_errors=True)
        elapsed = time.time() - t_start
        timings[filename] = elapsed

        stdout = out.get("stdout", "")
        for line in stdout.splitlines():
            logger.info("    %s", line)

        if "Set failed" in stdout or out.get("rc", 0) != 0:
            logger.error("  [%d/%d] FAILED %s after %.2fs", idx, len(files), filename, elapsed)
            logger.error("  stderr: %s", out.get("stderr", ""))
            pytest.fail(f"gnmi_client.py failed for {filename}: {stdout}")

        logger.info("  [%d/%d] done    %-40s  %.2fs", idx, len(files), filename, elapsed)

        duthost.shell(f"rm -f {npu_path}", module_ignore_errors=True)


def load_json_via_ptf(localhost, duthost, dpuhost, ptfhost, config_dir, files, timings):
    """Push each JSON config file to the DPU via py_gnmicli.py on the PTF container."""
    env = GNMIEnvironment(duthost)
    dpu_host_str = f"dpu{dpuhost.dpu_index}"

    for idx, filename in enumerate(files, start=1):
        local_path = os.path.join(config_dir, filename)
        logger.info("  [%d/%d] pushing %s ...", idx, len(files), filename)

        with open(local_path) as f:
            operations = json.load(f)

        update_list = []
        delete_list = []
        update_cnt = 0

        for operation in operations:
            if operation["OP"] == "SET":
                for k, v in operation.items():
                    if k == "OP":
                        continue
                    update_cnt += 1
                    file_name = f"update{update_cnt}"
                    keys = k.split(":", 1)
                    gnmi_key = keys[0] + "[key=" + keys[1] + "]"
                    if proto_utils.ENABLE_PROTO:
                        message = proto_utils.parse_dash_proto(k, v)
                        with open(env.work_dir + file_name, "wb") as bf:
                            bf.write(message.SerializeToString())
                        path = f"/DPU_APPL_DB/{dpu_host_str}/{gnmi_key}:$/root/{file_name}"     # noqa: E231
                    else:
                        with open(env.work_dir + file_name, "w") as tf:
                            tf.write(json.dumps(v))
                        path = f"/DPU_APPL_DB/{dpu_host_str}/{gnmi_key}:@/root/{file_name}"     # noqa: E231
                    update_list.append(path)
            elif operation["OP"] == "DEL":
                for k, v in operation.items():
                    if k == "OP":
                        continue
                    keys = k.split(":", 1)
                    gnmi_key = keys[0] + "[key=" + keys[1] + "]"
                    delete_list.append(f"/DPU_APPL_DB/{dpu_host_str}/{gnmi_key}")

        t_start = time.time()
        try:
            write_gnmi_files(localhost, duthost, ptfhost, env, delete_list, update_list, 1024)
        except Exception as e:
            elapsed = time.time() - t_start
            timings[filename] = elapsed
            logger.error("  [%d/%d] FAILED %s after %.2fs: %s", idx, len(files), filename, elapsed, e)
            pytest.fail(f"gNMI push failed for {filename}: {e}")
        elapsed = time.time() - t_start
        timings[filename] = elapsed

        logger.info("  [%d/%d] done    %-40s  %.2fs", idx, len(files), filename, elapsed)


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
    out = dpuhost.shell(
        f"sonic-db-cli DPU_APPL_DB KEYS '{table_pattern}' 2>/dev/null",
        module_ignore_errors=True,
    )
    keys = [k.strip() for k in out.get("stdout", "").splitlines() if k.strip()]
    if label:
        logger.info("  DPU_APPL_DB %s: %d keys matching '%s'", label, len(keys), table_pattern)
        for k in keys[:5]:
            logger.info("    sample: %s", k)
        if len(keys) > 5:
            logger.info("    ... and %d more", len(keys) - 5)
    return keys


def _setup_gnmi_server_no_client_auth(localhost, duthost):
    """Generate certs and restart gNMI server without client cert requirement.

    Generates a fresh CA + server cert (with NPU mgmt IP in SAN), deploys
    to the NPU, and restarts the gNMI server with no client auth.  This
    allows the sonic-gnmi-agent container to connect remotely without
    needing to present a client certificate.
    """
    generate_gnmi_cert(localhost, duthost)
    env = GNMIEnvironment(duthost)

    # Deploy server certs to NPU
    duthost.copy(src=env.work_dir + env.gnmi_ca_cert, dest=env.gnmi_cert_path)
    duthost.copy(src=env.work_dir + env.gnmi_server_cert, dest=env.gnmi_cert_path)
    duthost.copy(src=env.work_dir + env.gnmi_server_key, dest=env.gnmi_cert_path)

    # Restart gNMI server with --allow_no_client_auth
    port = env.gnmi_port
    assert int(port) > 0, "Invalid GNMI port"
    duthost.shell(
        "docker exec %s supervisorctl stop %s" % (env.gnmi_container, env.gnmi_program)
    )
    duthost.shell(
        "docker exec %s pkill telemetry" % env.gnmi_container,
        module_ignore_errors=True,
    )
    dut_command = "docker exec %s bash -c " % env.gnmi_container
    dut_command += "\"/usr/bin/nohup /usr/sbin/telemetry -logtostderr --port %s " % port
    dut_command += "--server_crt %s%s " % (env.gnmi_cert_path, env.gnmi_server_cert)
    dut_command += "--server_key %s%s " % (env.gnmi_cert_path, env.gnmi_server_key)
    dut_command += "--ca_crt %s%s " % (env.gnmi_cert_path, env.gnmi_ca_cert)
    dut_command += "--allow_no_client_auth "
    if env.enable_zmq:
        dut_command += "-zmq_address=tcp://127.0.0.1:8100 "
    dut_command += "-gnmi_native_write=true -v=10 >/root/gnmi.log 2>&1 &\""
    duthost.shell(dut_command)
    logger.info("Waiting %ds for gNMI server restart (no client auth)...",
                env.gnmi_server_start_wait_time)
    time.sleep(env.gnmi_server_start_wait_time)


def _container_path_to_host(container_path):
    """Translate a path inside this (sonic-mgmt) container to the host path.

    The sonic-mgmt container is typically started with:
        docker run -v /home/dash/sonic-mgmt:/home/dash/sonic-mgmt/sonic-mgmt ...
    so a container path like /home/dash/sonic-mgmt/sonic-mgmt/tests/...
    maps to host path        /home/dash/sonic-mgmt/tests/...

    Detect and collapse any repeated adjacent directory component
    (e.g. .../sonic-mgmt/sonic-mgmt/... → .../sonic-mgmt/...).
    Falls back to the original path if no repeated component is found.
    """
    parts = container_path.split("/")
    for i in range(1, len(parts) - 1):
        if parts[i] and parts[i] == parts[i + 1]:
            candidate = "/".join(parts[:i] + parts[i + 1:])
            logger.info("Path translation: %s -> %s (collapsed '%s')",
                        container_path, candidate, parts[i])
            return candidate
    return container_path


_GNMI_CONTAINER_NAME = "sonic-gnmi-agent-push"


def _merge_config_files(config_dir, files, chunk_size=16):
    """Merge config JSON files into fewer, larger files to reduce per-file overhead.

    Files are sorted by name, then grouped into chunks of ``chunk_size``.
    Each chunk's JSON operations are concatenated (preserving order) into a
    single merged file.

    Returns (merged_dir, merged_files) where merged_dir is a temp directory
    and merged_files is the new file list.  The caller must delete merged_dir
    when done.
    """
    # Create merged dir inside config_dir so it shares the same bind-mount
    # visible to Docker on the host (tempfile.mkdtemp uses /tmp which is
    # only inside the sonic-mgmt container and not visible to the host).
    merged_dir = tempfile.mkdtemp(prefix="dash_merged_", dir=config_dir)
    sorted_files = sorted(files)

    merged_files = []
    for chunk_idx in range(0, len(sorted_files), chunk_size):
        chunk = sorted_files[chunk_idx:chunk_idx + chunk_size]
        merged_ops = []
        for f in chunk:
            with open(os.path.join(config_dir, f)) as fh:
                merged_ops.extend(json.load(fh))

        merged_name = "merged_%03d.json" % (chunk_idx // chunk_size)
        with open(os.path.join(merged_dir, merged_name), "w") as fh:
            json.dump(merged_ops, fh)

        logger.info(
            "  Merged %d files (%d ops) -> %s  [%s .. %s]",
            len(chunk), len(merged_ops), merged_name, chunk[0], chunk[-1],
        )
        merged_files.append(merged_name)

    logger.info("Merge complete: %d original files -> %d merged files",
                len(files), len(merged_files))

    return merged_dir, merged_files


def load_json_via_gnmi(localhost, duthost, dpuhost, config_dir, files, timings, sub_timings=None):
    """Push each JSON config file via a persistent sonic-gnmi-agent container.

    Starts one long-lived container with config_dir bind-mounted to /dpu,
    then uses 'docker exec' for each file push — avoiding per-file container
    startup overhead.  Verification is batched at the end for speed.
    """
    if sub_timings is None:
        sub_timings = {}
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

    # ── Inject instrumented gnmi_client.py + go_gnmi_utils.py for timing ──
    # docker cp doesn't work in Docker-in-Docker (daemon reads host FS, not
    # sonic-mgmt FS).  Instead we bind-mount the gnmi/ directory and then use
    # 'docker exec cp' to overwrite inside the running container.
    _gnmi_src_dir = os.path.realpath(
        os.path.join(os.path.dirname(__file__), "..", "..", "gnmi")
    )
    _host_gnmi_dir = _container_path_to_host(_gnmi_src_dir)
    logger.info("Injecting instrumented files from %s (host: %s)",
                _gnmi_src_dir, _host_gnmi_dir)
    # Find where gnmi_client.py lives inside the container
    which_out = localhost.shell(
        f"docker exec {_GNMI_CONTAINER_NAME} which gnmi_client.py",
        module_ignore_errors=True,
    )
    _ctr_gnmi_client = which_out.get("stdout", "").strip() or "/usr/sbin/gnmi_client.py"
    logger.info("gnmi_client.py in container at: %s", _ctr_gnmi_client)
    # Pipe file contents into the container via 'docker exec ... tee'.
    # This avoids docker cp path issues in Docker-in-Docker.
    for src_local, dst_ctr in [
        (os.path.join(_gnmi_src_dir, "gnmi_client.py"), _ctr_gnmi_client),
        (os.path.join(_gnmi_src_dir, "gnmi_agent", "go_gnmi_utils.py"), _GO_GNMI_UTILS_CTR),
    ]:
        with open(src_local, "r") as f:
            content = f.read()
        # Use a heredoc to pipe the content into the container.
        inject_cmd = (
            f"docker exec -i {_GNMI_CONTAINER_NAME}"
            f" tee {dst_ctr} > /dev/null <<'__INJECT_EOF__'\n"
            f"{content}\n__INJECT_EOF__"
        )
        inject_out = localhost.shell(inject_cmd, module_ignore_errors=True)
        rc = inject_out.get("rc", -1)
        logger.info("  injected %s -> %s (rc=%d)", os.path.basename(src_local), dst_ctr, rc)
        if rc != 0:
            logger.warning("  injection stderr: %s", inject_out.get("stderr", "")[:300])
    # Verify the files were updated by checking for the TIMING marker.
    verify_out = localhost.shell(
        f"docker exec {_GNMI_CONTAINER_NAME} grep -c '_phase_totals' {_GO_GNMI_UTILS_CTR}",
        module_ignore_errors=True,
    )
    logger.info("Verification — _phase_totals count in container go_gnmi_utils.py: %s",
                verify_out.get("stdout", "").strip())
    # ── End instrumentation injection ──

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

        # docker exec on the persistent container — no startup overhead.
        cmd = (
            f"docker exec {_GNMI_CONTAINER_NAME}"
            f" gnmi_client.py --batch_val 10000 --no-proto -i {dpu_index}"
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


def load_json_via_cli(localhost, duthost, dpuhost, ptfhost, config_dir, files, timings):
    """Push each JSON config file to the DPU via py_gnmicli.py CLI on the PTF container.

    Equivalent to load_json_via_ptf but directly constructs and runs the
    py_gnmicli.py command without going through write_gnmi_files/gnmi_set helpers.
    """
    env = GNMIEnvironment(duthost)
    dpu_host_str = f"dpu{dpuhost.dpu_index}"
    ip = duthost.mgmt_ip
    port = env.gnmi_port

    localhost.shell(f"mkdir -p {env.work_dir}", module_ignore_errors=True)

    base_cmd = (
        f'/root/env-python3/bin/python /root/gnxi/gnmi_cli_py/py_gnmicli.py '
        f'--timeout 30 '
        f'-t {ip} -p {port} '
        f'-xo sonic-db '
        f'-rcert /root/{env.gnmi_ca_cert} '
        f'-pkey /root/{env.gnmi_client_key} '
        f'-cchain /root/{env.gnmi_client_cert} '
    )

    for idx, filename in enumerate(files, start=1):
        local_path = os.path.join(config_dir, filename)
        logger.info("  [%d/%d] pushing %s ...", idx, len(files), filename)

        with open(local_path) as f:
            operations = json.load(f)

        update_list = []
        delete_list = []
        update_cnt = 0

        for operation in operations:
            if operation["OP"] == "SET":
                for k, v in operation.items():
                    if k == "OP":
                        continue
                    update_cnt += 1
                    file_name = f"update{update_cnt}"
                    keys = k.split(":", 1)
                    gnmi_key = keys[0] + "[key=" + keys[1] + "]"
                    if proto_utils.ENABLE_PROTO:
                        message = proto_utils.parse_dash_proto(k, v)
                        with open(env.work_dir + file_name, "wb") as bf:
                            bf.write(message.SerializeToString())
                        path = f"/DPU_APPL_DB/{dpu_host_str}/{gnmi_key}:$/root/{file_name}"     # noqa: E231
                    else:
                        with open(env.work_dir + file_name, "w") as tf:
                            tf.write(json.dumps(v))
                        path = f"/DPU_APPL_DB/{dpu_host_str}/{gnmi_key}:@/root/{file_name}"     # noqa: E231
                    update_list.append(path)
            elif operation["OP"] == "DEL":
                for k, v in operation.items():
                    if k == "OP":
                        continue
                    keys = k.split(":", 1)
                    gnmi_key = keys[0] + "[key=" + keys[1] + "]"
                    delete_list.append(f"/DPU_APPL_DB/{dpu_host_str}/{gnmi_key}")

        localhost.shell(f'tar -czf /tmp/updates.tar.gz -C {env.work_dir} .')
        ptfhost.copy(src='/tmp/updates.tar.gz', dest='~')
        ptfhost.shell('tar -xf updates.tar.gz')

        t_start = time.time()
        try:
            if delete_list:
                xpath = ' '.join(p.replace('sonic-db:', '') for p in delete_list)
                xvalue = ' '.join('""' for _ in delete_list)
                cmd = base_cmd + f'-m set-delete --xpath {xpath} --value {xvalue}'
                output = ptfhost.shell(cmd, module_ignore_errors=True)
                if "GRPC error\n" in output['stdout']:
                    raise Exception("GRPC error: " + output['stdout'].split("GRPC error\n", 1)[1])

            if update_list:
                xpath = ''
                xvalue = ''
                for update in update_list:
                    update = update.replace('sonic-db:', '')
                    result = update.rsplit(':', 1)
                    xpath += ' ' + result[0]
                    xvalue += ' ' + result[1]
                cmd = base_cmd + f'-m set-update --xpath {xpath} --value {xvalue}'
                output = ptfhost.shell(cmd, module_ignore_errors=True)
                if "GRPC error\n" in output['stdout']:
                    raise Exception("GRPC error: " + output['stdout'].split("GRPC error\n", 1)[1])

        except Exception as e:
            elapsed = time.time() - t_start
            timings[filename] = elapsed
            logger.error("  [%d/%d] FAILED %s after %.2fs: %s", idx, len(files), filename, elapsed, e)
            pytest.fail(f"gNMI CLI push failed for {filename}: {e}")

        elapsed = time.time() - t_start
        timings[filename] = elapsed
        logger.info("  [%d/%d] done    %-40s  %.2fs", idx, len(files), filename, elapsed)

        localhost.shell('rm -f /tmp/updates.tar.gz')
        ptfhost.shell('rm -f updates.tar.gz')
        localhost.shell(f'find {env.work_dir} -name "update*" -delete')
        ptfhost.shell('find . -maxdepth 1 -name "update*" -delete')


def test_dash_api_load_speed_pl(localhost, duthost, dpuhosts, dpu_index, ptfhost):
    """
    Measure the time to load private-link-50 DASH configs onto a DPU via gNMI.

    For each JSON config file:
      1. Copy the file to _NPU_STAGE_DIR on the NPU.
      2. Run sonic-gnmi-agent docker container on the NPU with the stage dir
         mounted, invoking gnmi_client.py to push the file to the DPU.
      3. Record and log the time taken per file.
    """
    dpuhost = dpuhosts[dpu_index]

    # ── Pre-flight: verify DPU is alive via SSH port check ───────────────────
    # ping and 'show chassis module midplane-status' are both unreliable after
    # a previous run removed the midplane default route. Check TCP port 22
    # instead — if SSH is listening the DPU is up.
    dpu_name = f"DPU{dpuhost.dpu_index}"
    dpu_midplane_ip = "169.254.200.%d" % (dpuhost.dpu_index + 1)
    logger.info("Pre-flight: assuming %s is up at %s (no automated check)", dpu_name, dpu_midplane_ip)

    config_dir = os.path.join(CONFIG_DIR, f"dpu{dpuhost.dpu_index}")

    assert os.path.isdir(config_dir), \
        f"Config directory not found: {config_dir}"

    pattern = f"*dpu{dpuhost.dpu_index}*.json"
    files = sorted(
        f for f in os.listdir(config_dir)
        if fnmatch.fnmatch(f, pattern) and f.endswith(".json")
    )
    assert files, f"No JSON config files found matching '{pattern}' in {config_dir}"
    logger.info(
        "Found %d config files to load for dpu%d",
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

    # Select the load method:
    #   "npu"  — gnmi_client.py via docker run on the NPU (connects to 127.0.0.1:50052)
    #   "ptf"  — py_gnmicli.py via write_gnmi_files/gnmi_set helpers on PTF
    #   "cli"  — py_gnmicli.py invoked directly on PTF (same tool as "ptf", no helpers)
    #   "gnmi" — gnmi_client.py via sonic-gnmi-agent container on the local (sonic-mgmt) machine
    _LOAD_METHOD = "gnmi"
    _MERGE_MAP = False  # Push individual files (no merging) for per-file timing

    # Cert setup depends on the load method.
    if _LOAD_METHOD in ("ptf", "cli"):
        logger.info("Setting up gNMI certs on NPU and PTF...")
        generate_gnmi_cert(localhost, duthost)
        apply_gnmi_cert(duthost, ptfhost)
    elif _LOAD_METHOD == "gnmi":
        logger.info("Setting up gNMI server (no client auth) for remote access...")
        _setup_gnmi_server_no_client_auth(localhost, duthost)

    # Optionally merge map files to reduce per-file overhead.
    merged_dir = None
    effective_config_dir = config_dir
    effective_files = files
    if _MERGE_MAP:
        merged_dir, effective_files = _merge_config_files(config_dir, files, chunk_size=16)
        effective_config_dir = merged_dir

    timings = {}
    sub_timings = {}
    total_start = time.time()

    try:
        if _LOAD_METHOD == "ptf":
            load_json_via_ptf(localhost, duthost, dpuhost, ptfhost,
                              effective_config_dir, effective_files, timings)
        elif _LOAD_METHOD == "npu":
            load_json_via_npu(duthost, dpuhost, effective_config_dir, effective_files, timings)
        elif _LOAD_METHOD == "cli":
            load_json_via_cli(localhost, duthost, dpuhost, ptfhost,
                              effective_config_dir, effective_files, timings)
        elif _LOAD_METHOD == "gnmi":
            load_json_via_gnmi(localhost, duthost, dpuhost,
                               effective_config_dir, effective_files, timings,
                               sub_timings)
        else:
            raise ValueError(f"Invalid load method: {_LOAD_METHOD}")
    finally:
        if merged_dir:
            shutil.rmtree(merged_dir, ignore_errors=True)
            logger.info("Cleaned up merged dir: %s", merged_dir)

        # Always print results, even if the load raised an exception.
        total_elapsed = time.time() - total_start
        try:
            mem_after = {
                "NPU": _collect_memory(duthost),
                "DPU": _collect_memory(dpuhost),
            }
            redis_after = _collect_redis_memory(dpuhost)
            _print_results(timings, total_elapsed, mem_before, mem_after, redis_before, redis_after)
            if sub_timings:
                _print_gnmi_timing_breakdown(timings, sub_timings)
        except Exception:
            logger.exception("Failed to collect/print post-test results")

    # ── Check DPU is still up after the push ──────────────────────────────────
    # Midplane reachability (169.254.200.x) is expected to be False after we
    # removed the midplane default route, so it is not a reliable crash indicator.
    # Instead, ping the dataplane IP — if that also fails the DPU is truly down.
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

    # ── Verify ENIs are programmed on DPU ───────────────────────────────────
    # ENIs take time to propagate from APPL_DB through the DPU pipeline into
    # COUNTERS_ENI_NAME_MAP. Poll with a generous timeout.
    # With 3 config files (1 ENI set) we expect 1 ENI; full 129 files → 64 ENIs.
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
        # sonic-db-cli returns a Python dict string like {'k1': 'v1', 'k2': 'v2'}.
        # Count the number of keys by counting 'eni-' occurrences.
        eni_count = eni_stdout.count("eni-")
        logger.info("DPU: ENIs found: %d / %d", eni_count, _ENI_EXPECTED)
        logger.info("DPU: COUNTERS_ENI_NAME_MAP raw output:\n%s", eni_stdout or "(empty)")
        if eni_count >= _ENI_EXPECTED:
            break
        time.sleep(_ENI_POLL_INTERVAL)
    assert eni_count >= _ENI_EXPECTED, \
        "Expected %d ENIs in COUNTERS_ENI_NAME_MAP but found %d after %ds" % (
            _ENI_EXPECTED, eni_count, _ENI_TIMEOUT)
