"""Shared helpers for the DASH API load-speed tests.

Both ``test_dash_api_speed_pl.py`` (gRPC-only timing) and
``test_dash_api_speed_pl_with_traffic.py`` (gRPC vs. live-traffic timing)
import from here so the config-push path is identical between them.

The push (:func:`load_json_via_gnmi`) optionally records a per-file
*push event* — start/end wall-clock and the parsed ENI index/kind — into a
caller-supplied ``push_events`` dict, so the traffic test can correlate when
each ENI's config finished pushing (the "gRPC time") against when its flow
started forwarding in hardware (the "traffic time").
"""
import json
import logging
import os
import re
import shlex
import time

import pytest
from gnmi_utils import GNMIEnvironment

logger = logging.getLogger(__name__)

# gnmi-agent container image
_GNMI_AGENT_IMAGE = "sonic-gnmi-agent:2026march13"

_GNMI_CONTAINER_NAME = "sonic-gnmi-agent-push"

_NPU_STATIC_ARP = [
    ("220.0.1.2", "80:09:02:02:00:01"),
    ("220.0.2.2", "80:09:02:02:00:02"),
    ("220.0.3.2", "80:09:02:02:00:03"),
    ("220.0.4.2", "80:09:02:02:00:04"),
]

# Filename pattern: pl_100.dpu0.001eni.json -> index 001, kind "eni".
_FILE_INDEX_RE = re.compile(r"\.(\d{3})(apl|eni|map)\.json$")


def parse_file_index(filename):
    """Return (index:int, kind:str) for a rendered config file, or (None, None)."""
    m = _FILE_INDEX_RE.search(filename)
    if not m:
        return None, None
    return int(m.group(1)), m.group(2)


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
    out = host.shell("docker stats --no-stream | awk 'NR>1 {print $2\"\\t\"$4}'", module_ignore_errors=True)
    for line in out.get("stdout", "").splitlines():
        line = line.strip()
        if not line or "\t" not in line:
            continue
        name, used_str = line.split("\t", 1)
        result[name.strip()] = _parse_mem_str(used_str.strip())

    result.update(_collect_free_memory(host))
    return result


def _collect_free_memory(host):
    """Run ``free -m`` on *host* and return a dict with system memory keys (MiB)."""
    result = {}
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
    return result


def _collect_redis_memory(dpuhost):
    """Redis memory info from DPU_APPL_DB: totals plus 2 VNET_MAPPING key samples."""
    result = {}

    info_out = dpuhost.shell("sonic-db-cli DPU_APPL_DB INFO MEMORY", module_ignore_errors=True)
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
        module_ignore_errors=True)
    for key in keys_out.get("stdout", "").splitlines():
        key = key.strip()
        if not key:
            continue
        usage_out = dpuhost.shell(f"sonic-db-cli DPU_APPL_DB MEMORY USAGE '{key}'", module_ignore_errors=True)
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
    if timings:
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
        logger.info("  %-30s  %8.1f  %8.1f  %+8.1f", "Containers total",
                    total_before, total_after, total_after - total_before)

        sys_b = before.get("_system_used", 0.0)
        sys_a = after.get("_system_used", 0.0)
        logger.info("  %-30s  %8.1f  %8.1f  %+8.1f", "System used (free -m)", sys_b, sys_a, sys_a - sys_b)
        sys_total = before.get("_system_total", after.get("_system_total", 0.0))
        for key, label in [("_system_free", "System free"), ("_system_available", "System available")]:
            b = before.get(key, 0.0)
            a = after.get(key, 0.0)
            logger.info("  %-30s  %8.1f  %8.1f  %+8.1f", label, b, a, a - b)
        if sys_total:
            logger.info("  %-30s  %8.1f", "System total", sys_total)

    # Memory timeline (per-file free memory after each push)
    if mem_timeline:
        logger.info("\n  Memory timeline — free memory after each file push (MiB):")
        logger.info("  %-6s  %-40s  %7s  %9s  %9s  %9s  %9s",
                    "#", "File", "Ops", "NPU free", "NPU avail", "DPU free", "DPU avail")
        logger.info("  " + "-" * 96)
        for entry in mem_timeline:
            logger.info("  %-6s  %-40s  %7d  %9.0f  %9.0f  %9.0f  %9.0f",
                        entry["idx"], entry["file"][:40], entry["ops"],
                        entry["npu_free"], entry["npu_available"],
                        entry["dpu_free"], entry["dpu_available"])
        # Summary: min free across all snapshots
        if len(mem_timeline) > 1:
            logger.info("  " + "-" * 96)
            logger.info("  %-6s  %-40s  %7s  %9.0f  %9.0f  %9.0f  %9.0f", "", "MINIMUM", "",
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
    logger.info("  %-52s  %10d  %10d  %+10d", "used_memory (total)",
                redis_b_total, redis_a_total, redis_a_total - redis_b_total)
    logger.info("  %-52s  %10s  %10s", "used_memory_human",
                redis_before.get("_used_memory_human", "n/a"),
                redis_after.get("_used_memory_human", "n/a"))

    sample_keys = sorted(k for k in set(redis_before) | set(redis_after) if not k.startswith("_"))
    for key in sample_keys:
        b = redis_before.get(key, 0)
        a = redis_after.get(key, 0)
        logger.info("  %-52s  %10d  %10d  %+10d", key, b, a, a - b)

    logger.info(sep)


def npu_pre_config(duthost, dpu_midplane_ip, dpu_dataplane_ip):
    """Prepare NPU for DASH push: static ARPs, midplane/dataplane ping, log routes/ifaces."""
    logger.info("NPU: adding permanent static ARP entries for dataplane next-hops")
    for ip, mac in _NPU_STATIC_ARP:
        route_out = duthost.shell(f"ip route get {ip}", module_ignore_errors=True)
        tokens = route_out.get("stdout", "").split()
        dev = next((tokens[i + 1] for i, t in enumerate(tokens) if t == "dev"), None)
        assert dev, f"Could not determine egress interface for {ip} on NPU"

        for attempt in range(3):
            duthost.shell(f"sudo ip neigh replace {ip} lladdr {mac} dev {dev} nud permanent", module_ignore_errors=True)
            verify = duthost.shell(f"ip neigh show {ip}", module_ignore_errors=True)
            if "PERMANENT" in verify.get("stdout", "").upper():
                logger.info("  NPU: permanent ARP %s lladdr %s dev %s (attempt %d)", ip, mac, dev, attempt + 1)
                break
        else:
            raise AssertionError(
                f"Failed to add permanent ARP entry for {ip} after 3 attempts. "
                f"'ip neigh show {ip}': {verify.get('stdout', '')}"
            )

    logger.info("NPU: pinging DPU midplane IP %s to populate ARP", dpu_midplane_ip)
    duthost.shell(f"ping -c 3 -W 2 {dpu_midplane_ip}", module_ignore_errors=True)

    arp_out = duthost.shell(f"ip n show {dpu_midplane_ip}", module_ignore_errors=True)
    logger.info("NPU ARP entry for %s: %s", dpu_midplane_ip, arp_out.get("stdout", "").strip() or "(none)")

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
    out = dpuhost.shell(f"sonic-db-cli DPU_APPL_DB KEYS '{table_pattern}' 2>/dev/null",
                        module_ignore_errors=True, verbose=not quiet)
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
            logger.info("Path translation: %s -> %s (collapsed '%s')", container_path, candidate, parts[i])
            return candidate
    return container_path


def _inspect_gnmi_server(config_facts):
    """Discover TLS mode and cert paths from the NPU's already-loaded CONFIG_DB.

    `client_auth` is the authoritative switch for mTLS: if it's true, the server
    will reject clients that don't present a cert, regardless of whether
    `GNMI.certs.ca_crt` is listed. Absence of server_crt/server_key means the
    server is running noTLS.

    The GNMI YANG model only accepts `server_crt`/`server_key`/`ca_crt` under
    `GNMI|certs` — `client_crt`/`client_key` break `config apply-patch`. So for
    mTLS we derive them by convention (`client.crt` / `client.key` alongside the
    CA or server cert) and let `_fetch_gnmi_certs_from_npu` skip any that are
    absent on disk.
    """
    gnmi_cfg = (config_facts or {}).get("GNMI", {}) or {}
    certs = gnmi_cfg.get("certs", {}) or {}
    gnmi = gnmi_cfg.get("gnmi", {}) or {}

    paths = {
        "server_crt": certs.get("server_crt"),
        "server_key": certs.get("server_key"),
        "ca_crt": certs.get("ca_crt"),
    }
    client_auth = str(gnmi.get("client_auth", "false")).lower() == "true"

    has_tls = bool(paths["server_crt"] and paths["server_key"])
    if not has_tls:
        mode = "insecure"
    elif client_auth:
        mode = "mtls"
    else:
        mode = "tls"

    if mode == "mtls":
        ref = paths["ca_crt"] or paths["server_crt"]
        if ref:
            cert_dir = os.path.dirname(ref)
            paths["client_crt"] = f"{cert_dir}/client.crt"
            paths["client_key"] = f"{cert_dir}/client.key"
            if not paths["ca_crt"]:
                paths["ca_crt"] = f"{cert_dir}/ca.crt"

    logger.info("CONFIG_DB GNMI.certs: %s", {k: v for k, v in paths.items() if v} or "(none)")
    logger.info("CONFIG_DB GNMI.gnmi.client_auth=%s → mode=%s", client_auth, mode)
    return {"mode": mode, "paths": paths, "client_auth": client_auth}


def _fetch_gnmi_certs_from_npu(duthost, env, paths, dest_dir):
    """Fetch only the cert files the server actually references (from CONFIG_DB).

    `paths` is the runtime mapping from `_inspect_gnmi_server()`. For each
    non-None entry we `docker cp` out of the gnmi container and `fetch` to
    dest_dir. Missing files are skipped with a warning — the server may not
    have all three (e.g. no ca_crt when client_auth is off). Returns
    {flag: basename} for every file successfully staged locally.
    """
    container = env.gnmi_container
    fetched = {}
    for flag, src in paths.items():
        if not src:
            continue
        name = os.path.basename(src)
        cp = duthost.shell(
            f"docker cp {container}:{src} /tmp/{name}",  # noqa: E231
            module_ignore_errors=True,
        )
        if cp.get("rc", 1) != 0:
            logger.warning("  docker cp %s failed, skipping: %s",
                           src, (cp.get("stderr", "") or "").strip()[:200])
            continue
        try:
            duthost.fetch(src=f"/tmp/{name}", dest=f"{dest_dir}/{name}", flat=True)
            fetched[flag] = name
            logger.info("  Fetched %s (%s) from %s", name, flag, src)
        except Exception as e:
            logger.warning("  fetch %s failed: %s", src, e)
    return fetched


def load_json_via_gnmi(localhost, duthost, dpuhost, config_facts, config_dir, files, timings,
                       creds, mem_timeline=None, push_events=None, on_file_done=None):
    """Push each JSON via a long-lived sonic-gnmi-agent container (config_dir mounted at /dpu).

    Optional instrumentation for the traffic test:
      * ``push_events`` — if a dict is supplied, records per-file
        ``{filename: {"idx": N, "kind": "eni"|"map"|"apl", "start": t0,
        "end": t1}}`` with wall-clock timestamps (time.time()).
      * ``on_file_done`` — if callable, invoked as
        ``on_file_done(filename, idx, kind, t_start, t_end)`` right after each
        file's push completes (lets the caller snapshot live traffic state).
    """
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

    # Mode + cert paths come from the NPU's loaded CONFIG_DB (module fixture),
    # so we don't re-query the switch or assume /etc/sonic/telemetry/.
    server = _inspect_gnmi_server(config_facts)
    server_mode = server["mode"]

    cert_mount_opt = ""
    # gnmi_set flag semantics (from `gnmi_set -help`):
    #   -notls     → plain TCP, no TLS at all  (matches server --noTLS)
    #   -insecure  → TLS handshake, skip server verification (still needs client cert
    #                if the server requires one)
    #   -cert/-key/-ca → present client material for mTLS
    tls_flags = " -notls"
    if server_mode in ("tls", "mtls"):
        # Stage whatever certs the server lists (CA is enough for tls-only; mtls
        # still needs a client cert+key from somewhere — flagged below if absent).
        cert_stage_dir = os.path.join(os.path.dirname(config_dir), ".gnmi_certs")
        localhost.shell(f"mkdir -p {cert_stage_dir}", module_ignore_errors=True)
        fetched = _fetch_gnmi_certs_from_npu(duthost, env, server["paths"], cert_stage_dir)
        host_cert_dir = _container_path_to_host(cert_stage_dir) if fetched else ""
        if fetched:
            logger.info("cert_stage_dir (container): %s", cert_stage_dir)
            logger.info("cert_stage_dir (host):      %s", host_cert_dir)
            cert_mount_opt = (
                f" --mount src={host_cert_dir},target=/certs,type=bind,readonly"  # noqa: E231
            )
        parts = []
        # Server verification: use CA if available, else skip with -insecure.
        if "ca_crt" in fetched:
            parts.append(f" -ca /certs/{fetched['ca_crt']}")
        else:
            parts.append(" -insecure")
        # Match hostname verification against the server cert's SAN. Server certs
        # in sonic-mgmt testbeds include the mgmt IP as an IP SAN, so passing the
        # mgmt IP as target_name satisfies Go's TLS hostname check against either
        # a DNS or IP SAN. gnmi_set defaults to "hostname.com" which never matches.
        parts.append(f" -target_name {ip}")
        # mTLS additionally requires the client to present its own cert.
        if server_mode == "mtls":
            if "client_crt" in fetched and "client_key" in fetched:
                parts.append(f" -cert /certs/{fetched['client_crt']}")
                parts.append(f" -key /certs/{fetched['client_key']}")
            else:
                logger.warning("mTLS server but no client cert/key available — "
                               "push will likely be rejected")
        tls_flags = "".join(parts)

    # Mount the repo's go_gnmi_utils.py over the one baked into the container image
    # (the image's copy hardcodes -insecure, which doesn't work with an mTLS server).
    # The patched copy honors GNMI_CA / GNMI_CLIENT_CERT / GNMI_CLIENT_KEY / GNMI_TARGET_NAME.
    repo_go_utils = os.path.abspath(os.path.join(
        os.path.dirname(__file__), "..", "..", "gnmi", "gnmi_agent", "go_gnmi_utils.py"
    ))
    go_utils_host = _container_path_to_host(repo_go_utils)
    go_utils_mount = (
        f" --mount src={go_utils_host},"  # noqa: E231
        f"target=/usr/lib/python3/dist-packages/gnmi_agent/go_gnmi_utils.py,type=bind,readonly"  # noqa: E231
    )

    # Export TLS material via env vars so the patched go_gnmi_utils.py can wire
    # up gnmi_set / gnmi_get with the right flags.
    env_opts = ""
    if server_mode in ("tls", "mtls") and cert_mount_opt:
        env_opts += f" -e GNMI_TARGET_NAME={ip}"
        if "ca_crt" in fetched:
            env_opts += f" -e GNMI_CA=/certs/{fetched['ca_crt']}"
        if server_mode == "mtls" and "client_crt" in fetched and "client_key" in fetched:
            env_opts += f" -e GNMI_CLIENT_CERT=/certs/{fetched['client_crt']}"
            env_opts += f" -e GNMI_CLIENT_KEY=/certs/{fetched['client_key']}"

    # Snapshot DPU_APPL_DB key count before pushing
    db_before = dpuhost.shell("sonic-db-cli DPU_APPL_DB DBSIZE", module_ignore_errors=True)
    logger.info("DPU_APPL_DB DBSIZE before push: %s", db_before.get("stdout", "").strip())

    # Start a persistent container (reuse if already running).
    localhost.shell(f"docker rm -f {_GNMI_CONTAINER_NAME}", module_ignore_errors=True)
    start_out = localhost.shell(
        f"docker run -d --name {_GNMI_CONTAINER_NAME} --network host"
        f" --shm-size=256m"
        f"{env_opts}"
        f" --mount src={host_config_dir},target=/dpu,type=bind,readonly"  # noqa: E231
        f"{cert_mount_opt}"
        f"{go_utils_mount}"
        f" {_GNMI_AGENT_IMAGE} -c 'sleep infinity'",
        module_ignore_errors=True,
    )
    if start_out.get("rc", 1) != 0:
        pytest.fail("Could not start %s: %s" % (_GNMI_CONTAINER_NAME, start_out.get("stderr", "")))
    logger.info("Started persistent container %s", _GNMI_CONTAINER_NAME)

    # Dump what the gnmi-agent container sees at /certs — lets us catch a stale
    # or mismatched CA before the pre-check fails with an opaque x509 error.
    if server_mode in ("tls", "mtls") and cert_mount_opt:
        probe = (
            "ls -la /certs/ 2>&1; "  # noqa: E702
            "for f in /certs/*; do "  # noqa: E702
            "echo ---$f---; "  # noqa: E702
            "md5sum \"$f\" 2>/dev/null; "  # noqa: E702
            "openssl x509 -in \"$f\" -noout -subject -issuer 2>/dev/null || true; "  # noqa: E702
            "done; "  # noqa: E702
            "echo ---chain-verify---; "  # noqa: E702
            "openssl verify -CAfile /certs/ca.cer /certs/server.cer 2>&1 || true; "  # noqa: E702
            "openssl verify -CAfile /certs/ca.cer /certs/client.crt 2>&1 || true; "  # noqa: E702
            "echo ---live-server-cert---; "  # noqa: E702
            f"echo | openssl s_client -connect {ip}:{port} -showcerts -servername {ip} "  # noqa: E231,E702
            "2>/dev/null | openssl x509 -noout -subject -issuer -fingerprint -sha256 2>&1 || true"
        )
        cert_ls = localhost.shell(
            f"docker exec {_GNMI_CONTAINER_NAME} sh -c {shlex.quote(probe)}",
            module_ignore_errors=True,
        )
        logger.info("Cert staging snapshot:\n%s", cert_ls.get("stdout", "") or cert_ls.get("stderr", ""))

    # Pre-count operations for all files (outside the timed loop).
    file_info = []
    all_tables = set()
    for filename in files:
        local_path = os.path.join(config_dir, filename)
        op_count, tables = _count_json_operations(local_path)
        file_info.append((filename, op_count, tables))
        all_tables.update(tables.keys())

    # ── Pre-check: verify gNMI server is reachable before pushing files ──
    logger.info("Pre-check: verifying gNMI connectivity to %s:%s (tls=%s)",
                ip, port, tls_flags.strip())
    gnmi_user = shlex.quote(creds["sonicadmin_user"])
    gnmi_pass = shlex.quote(creds["sonicadmin_password"])
    check_cmd = (
        f"docker exec {_GNMI_CONTAINER_NAME}"
        f" /usr/sbin/gnmi_set -target_addr {ip}:{port}"  # noqa: E231
        f"{tls_flags}"
        f" -username {gnmi_user} -password {gnmi_pass}"
    )
    check_out = localhost.shell(check_cmd, module_ignore_errors=True)
    # An empty setRequest always fails on the server side ("Translib write is
    # disabled" / "Unimplemented"), so rc != 0 is expected. What we're actually
    # checking is whether the TLS+auth handshake completed — i.e. the server
    # replied at all. Connection-level failures (port closed, bad cert, bad
    # creds) must still abort.
    stderr = check_out.get("stderr", "") or ""
    stdout = check_out.get("stdout", "") or ""
    rc = check_out.get("rc", -1)
    logger.info("Pre-check result: rc=%s", rc)
    if stdout:
        logger.info("Pre-check stdout: %s", stdout[:1000])
    if stderr:
        logger.info("Pre-check stderr: %s", stderr[:1000])
    unreachable_markers = (
        "connection refused",
        "no route to host",
        "handshake failed",
        "x509:",
        "transport: Error while dialing",
        "authentication failed",
        "PermissionDenied",
    )
    if any(m.lower() in stderr.lower() for m in unreachable_markers):
        pytest.fail(
            f"gNMI server unreachable at {ip}:{port} — aborting.\n"  # noqa: E231
            f"stderr: {stderr[:500]}"
        )
    logger.info("Pre-check: gNMI server reachable (server replied past TLS/auth)")

    push_errors = []

    for idx, (filename, op_count, tables) in enumerate(file_info, start=1):
        table_summary = ", ".join(
            "{0}:{1}S/{2}D".format(t, tables[t]['SET'], tables[t]['DEL'])
            for t in sorted(tables)
        )
        logger.info("  [%d/%d] pushing %s (%d ops: %s) ...", idx, len(files), filename, op_count, table_summary)

        cmd = (
            f"docker exec {_GNMI_CONTAINER_NAME}"
            f" gnmi_client.py --batch_val 10000 --no-proto -i {dpu_index}"
            f" -n 8 -t {ip}:{port} update -f /dpu/{filename}"  # noqa: E231
        )

        t_start = time.time()
        out = localhost.shell(cmd, module_ignore_errors=True)
        t_end = time.time()
        elapsed = t_end - t_start
        timings[filename] = elapsed

        # Record per-file push event (wall-clock) for traffic correlation.
        eni_idx, kind = parse_file_index(filename)
        if push_events is not None:
            push_events[filename] = {
                "idx": eni_idx, "kind": kind, "start": t_start, "end": t_end,
            }
        if on_file_done is not None:
            try:
                on_file_done(filename, eni_idx, kind, t_start, t_end)
            except Exception:
                logger.exception("  on_file_done callback failed (non-fatal)")

        rc = out.get("rc", -1)
        stderr = out.get("stderr", "")

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
                         idx, len(files), filename, elapsed, failure_reason, stderr[-3000:])
            push_errors.append(f"{filename}: {failure_reason}")
            # Fail fast — if the first file fails, no point continuing
            if idx == 1:
                logger.error("First file failed — aborting remaining files")
                break
        else:
            logger.info("  [%d/%d] done    %-40s  %.2fs  rc=%d", idx, len(files), filename, elapsed, rc)

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
    localhost.shell(f"docker rm -f {_GNMI_CONTAINER_NAME}", module_ignore_errors=True)

    # Batch verification: check DPU_APPL_DB once for all tables.
    for table in sorted(all_tables):
        _verify_dpu_appl_db(dpuhost, "%s:*" % table, label="after all files")

    # Final DB size check
    db_after = dpuhost.shell("sonic-db-cli DPU_APPL_DB DBSIZE", module_ignore_errors=True)
    logger.info("DPU_APPL_DB DBSIZE after push: %s (was: %s)",
                db_after.get("stdout", "").strip(), db_before.get("stdout", "").strip())

    if push_errors:
        pytest.fail("gNMI push had %d error(s):\n%s" % (
            len(push_errors), "\n".join("  - %s" % e for e in push_errors)))
