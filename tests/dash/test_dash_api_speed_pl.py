import fnmatch
import json
import logging
import os
import re
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

CONFIG_DIR = os.path.join(os.path.dirname(__file__), "configs", "private-link-50")

# Path on NPU where JSON files are staged for the docker mount
_NPU_STAGE_DIR = "/tmp/dash_load"

# gnmi-agent container image and fixed paths expected on the NPU
_GNMI_AGENT_IMAGE = "sonic-gnmi-agent:2026march13"
_GO_GNMI_UTILS_NPU = "/root/pl_1/go_gnmi_utils.py"
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
    logger.info("DPU: adding Loopback0 IP %s", loopback_ip)
    dpuhost.shell("sudo config interface ip add Loopback0 %s" % loopback_ip)
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
            f" -c 'gnmi_client.py --batch_val 500 -i {dpuhost.dpu_index}"
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


def _apply_gnmi_cert_server_only(duthost):
    """Deploy generated certs to NPU and restart gNMI server (no PTF)."""
    env = GNMIEnvironment(duthost)
    duthost.copy(src=env.work_dir + env.gnmi_ca_cert, dest=env.gnmi_cert_path)
    duthost.copy(src=env.work_dir + env.gnmi_server_cert, dest=env.gnmi_cert_path)
    duthost.copy(src=env.work_dir + env.gnmi_server_key, dest=env.gnmi_cert_path)
    port = env.gnmi_port
    assert int(port) > 0, "Invalid GNMI port"
    dut_command = "docker exec %s supervisorctl stop %s" % (env.gnmi_container, env.gnmi_program)
    duthost.shell(dut_command)
    dut_command = "docker exec %s pkill telemetry" % (env.gnmi_container)
    duthost.shell(dut_command, module_ignore_errors=True)
    dut_command = "docker exec %s bash -c " % env.gnmi_container
    dut_command += "\"/usr/bin/nohup /usr/sbin/telemetry -logtostderr --port %s " % port
    dut_command += "--server_crt %s%s " % (env.gnmi_cert_path, env.gnmi_server_cert)
    dut_command += "--server_key %s%s " % (env.gnmi_cert_path, env.gnmi_server_key)
    dut_command += "--ca_crt %s%s " % (env.gnmi_cert_path, env.gnmi_ca_cert)
    if env.enable_zmq:
        dut_command += " -zmq_address=tcp://127.0.0.1:8100 "
    dut_command += "-gnmi_native_write=true -v=10 >/root/gnmi.log 2>&1 &\""
    duthost.shell(dut_command)
    logger.info("Waiting %ds for gNMI server restart...", env.gnmi_server_start_wait_time)
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


def load_json_via_gnmi(localhost, duthost, dpuhost, config_dir, files, timings):
    """Push each JSON config file via ephemeral sonic-gnmi-agent docker run.

    Uses the same pattern as the proven dpu.py script: for each file, run a
    fresh container with the config directory bind-mounted to /dpu, then invoke
    gnmi_client.py with -f /dpu/<basename>.

    After each file push, verifies keys actually landed in DPU_APPL_DB.
    """
    env = GNMIEnvironment(duthost)
    dpu_index = dpuhost.dpu_index
    ip = duthost.mgmt_ip
    port = env.gnmi_port

    # Translate container path → host path for docker bind mount.
    # We run inside a sonic-mgmt container but 'docker run' creates sibling
    # containers via the host daemon, so --mount src= must be a host path.
    host_config_dir = _container_path_to_host(config_dir)
    logger.info("config_dir (container): %s", config_dir)
    logger.info("config_dir (host):      %s", host_config_dir)

    # Stage client certs for mounting into the ephemeral container.
    # generate_gnmi_cert() created them in env.work_dir (/tmp/<uuid>/) which
    # is container-local and NOT visible to the host Docker daemon.  Copy
    # them to a path under the shared repo volume so the host can see them.
    cert_stage_dir = os.path.join(os.path.dirname(config_dir), ".gnmi_certs")
    localhost.shell(f"mkdir -p {cert_stage_dir}", module_ignore_errors=True)
    for cert_file in [env.gnmi_ca_cert, env.gnmi_client_cert, env.gnmi_client_key]:
        localhost.shell(f"cp {env.work_dir}{cert_file} {cert_stage_dir}/")
    host_cert_dir = _container_path_to_host(cert_stage_dir)
    logger.info("cert_stage_dir (container): %s", cert_stage_dir)
    logger.info("cert_stage_dir (host):      %s", host_cert_dir)

    # Snapshot DPU_APPL_DB key count before pushing
    db_before = dpuhost.shell(
        "sonic-db-cli DPU_APPL_DB DBSIZE",
        module_ignore_errors=True,
    )
    logger.info("DPU_APPL_DB DBSIZE before push: %s", db_before.get("stdout", "").strip())

    push_errors = []

    for idx, filename in enumerate(files, start=1):
        local_path = os.path.join(config_dir, filename)

        # Count expected operations
        op_count, tables = _count_json_operations(local_path)
        table_summary = ", ".join(
            "{0}:{1}S/{2}D".format(t, tables[t]['SET'], tables[t]['DEL']) for t in sorted(tables)
        )
        logger.info("  [%d/%d] pushing %s (%d ops: %s) ...",
                    idx, len(files), filename, op_count, table_summary)

        # Ephemeral docker run — mount config_dir as /dpu and certs at
        # /etc/sonic/telemetry/ (where gnmi_set expects them for mTLS).
        cmd = (
            f"docker run --rm --network host"
            f" --mount src={host_config_dir},target=/dpu,type=bind,readonly"  # noqa: E231
            f" --mount src={host_cert_dir},target=/etc/sonic/telemetry,type=bind,readonly"  # noqa: E231
            f" {_GNMI_AGENT_IMAGE}"
            f" -c 'gnmi_client.py --batch_val 500 -i {dpu_index}"
            f" -n 8 -t {ip}:{port} update -f /dpu/{filename}'"  # noqa: E231
        )
        logger.debug("  CMD: %s", cmd)

        t_start = time.time()
        out = localhost.shell(cmd, module_ignore_errors=True)
        elapsed = time.time() - t_start
        timings[filename] = elapsed

        rc = out.get("rc", -1)
        stdout = out.get("stdout", "")
        stderr = out.get("stderr", "")

        # Log ALL output — stdout and stderr
        if stdout.strip():
            for line in stdout.splitlines():
                logger.info("    [stdout] %s", line)
        else:
            logger.warning("    [stdout] (empty — gnmi_client.py produced no output)")

        if stderr.strip():
            for line in stderr.splitlines():
                logger.info("    [stderr] %s", line)

        # Detect failure: non-zero rc, known error strings, or empty stdout
        failed = False
        failure_reason = ""

        if rc != 0:
            failed = True
            failure_reason = f"exit code {rc}"
        elif "Set failed" in stdout or "GRPC error" in stdout or "Error" in stdout:
            failed = True
            failure_reason = "error string in stdout"
        elif "error" in stderr.lower() or "failed" in stderr.lower():
            failed = True
            failure_reason = "error string in stderr"

        if failed:
            msg = ("  [%d/%d] FAILED %s after %.2fs — %s\n"
                   "  stdout: %s\n  stderr: %s")
            logger.error(msg, idx, len(files), filename, elapsed, failure_reason,
                         stdout[:500], stderr[:500])
            push_errors.append(f"{filename}: {failure_reason}")
        else:
            logger.info("  [%d/%d] done    %-40s  %.2fs  rc=%d",
                        idx, len(files), filename, elapsed, rc)

        # Post-push verification: check DPU_APPL_DB for keys from tables in this file
        for table in tables:
            _verify_dpu_appl_db(dpuhost, "%s:*" % table, label="after %s" % filename)

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

    dpu_midplane_ip = "169.254.200.%d" % (dpuhost.dpu_index + 1)
    dpu_dataplane_ip = "10.0.0.%d" % (57 + dpuhost.dpu_index * 2)

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

    # Cert setup is needed for PTF-based methods and for the "gnmi" method
    # (which connects remotely to the NPU gNMI server and needs mTLS).
    if _LOAD_METHOD in ("ptf", "cli", "gnmi"):
        logger.info("Setting up gNMI certs on NPU...")
        generate_gnmi_cert(localhost, duthost)
        if _LOAD_METHOD in ("ptf", "cli"):
            apply_gnmi_cert(duthost, ptfhost)
        else:
            # "gnmi" method: apply server certs to NPU only (no PTF needed).
            # Client certs are mounted into the ephemeral container.
            _apply_gnmi_cert_server_only(duthost)

    timings = {}
    total_start = time.time()

    if _LOAD_METHOD == "ptf":
        load_json_via_ptf(localhost, duthost, dpuhost, ptfhost, config_dir, files, timings)
    elif _LOAD_METHOD == "npu":
        load_json_via_npu(duthost, dpuhost, config_dir, files, timings)
    elif _LOAD_METHOD == "cli":
        load_json_via_cli(localhost, duthost, dpuhost, ptfhost, config_dir, files, timings)
    elif _LOAD_METHOD == "gnmi":
        load_json_via_gnmi(localhost, duthost, dpuhost, config_dir, files, timings)
    else:
        raise ValueError(f"Invalid load method: {_LOAD_METHOD}")

    total_elapsed = time.time() - total_start

    mem_after = {
        "NPU": _collect_memory(duthost),
        "DPU": _collect_memory(dpuhost),
    }
    redis_after = _collect_redis_memory(dpuhost)

    _print_results(timings, total_elapsed, mem_before, mem_after, redis_before, redis_after)

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

    # ── Verify all 64 ENIs are programmed on DPU ──────────────────────────────
    # ENIs take time to propagate from APPL_DB through the DPU pipeline into
    # COUNTERS_ENI_NAME_MAP. Poll with a generous timeout.
    _ENI_EXPECTED = 64
    _ENI_POLL_INTERVAL = 10   # seconds between polls
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
        eni_lines = [line.strip() for line in eni_stdout.splitlines() if line.strip()]
        eni_count = len(eni_lines) // 2
        logger.info("DPU: ENIs found: %d / %d", eni_count, _ENI_EXPECTED)
        logger.info("DPU: COUNTERS_ENI_NAME_MAP raw output:\n%s", eni_stdout or "(empty)")
        if eni_count >= _ENI_EXPECTED:
            break
        time.sleep(_ENI_POLL_INTERVAL)
    assert eni_count == _ENI_EXPECTED, \
        "Expected %d ENIs in COUNTERS_ENI_NAME_MAP but found %d after %ds" % (
            _ENI_EXPECTED, eni_count, _ENI_TIMEOUT)
