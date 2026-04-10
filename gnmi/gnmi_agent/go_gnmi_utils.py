import json
import logging
import proto_utils
import time
import subprocess
import shutil
import os
import concurrent.futures


TIME_BETWEEN_CHUNKS = 1

# ── Instrumentation helpers ──────────────────────────────────────────
_phase_totals = {}   # phase_name -> cumulative seconds


def _record(phase, elapsed):
    _phase_totals.setdefault(phase, 0.0)
    _phase_totals[phase] += elapsed


def _log_timing_summary():
    logging.info("=" * 60)
    logging.info("TIMING BREAKDOWN (cumulative seconds per phase)")
    logging.info("-" * 60)
    for phase in ("json_load", "template_render", "proto_serialize",
                  "proto_file_write", "cmd_build", "gnmi_set_subprocess",
                  "proto_cleanup", "pipeline_wait", "sleep"):
        val = _phase_totals.get(phase, 0.0)
        if val > 0:
            logging.info("  %-25s %10.3f s", phase, val)
    total = sum(_phase_totals.values())
    logging.info("-" * 60)
    logging.info("  %-25s %10.3f s", "TOTAL accounted", total)
    logging.info("=" * 60)
# ── End instrumentation helpers ──────────────────────────────────────


class GNMIEnvironment:
    gnmi_ip = "127.0.0.1"
    gnmi_port = 8080
    work_dir = "/dev/shm/gnmi_work/"
    username = "admin"
    password = "password"
    dpu_index = 0
    num_dpus = 1


def exec_cmd(cmd):
    logging.debug(cmd)
    result = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    # Check the result
    if result.returncode == 0:
        logging.debug("Command executed successfully.")
        logging.debug("Output:")
        logging.debug(result.stdout)
    else:
        logging.error("Error executing the command.")
        logging.error("Error output (first 500 chars):")
        logging.error(result.stderr[:500])
    return result


def cleanup_proto_files(cmd_list, work_dir=None):
    if not cmd_list:
        return
    if work_dir:
        # Bulk wipe: one rmtree + mkdir beats 10K individual unlinks on tmpfs
        shutil.rmtree(work_dir, ignore_errors=True)
        os.makedirs(work_dir, exist_ok=True)
    else:
        for cmd in cmd_list:
            del_file = cmd.split("$")[-1]
            if del_file != '':
                logging.debug("Deleting file:" + del_file)
                os.unlink(del_file)


# Max command length (bytes) before we split into sub-calls.
# Linux MAX_ARG_STRLEN limits each individual execve() string to
# PAGE_SIZE*32 = 128 KB.  With shell=True the entire command is one
# string passed to /bin/sh -c, so we must stay under that limit.
_MAX_CMD_BYTES = 120_000


def _build_gnmi_set_cmd(env, delete_list, update_list, replace_list):
    """Build the gnmi_set CLI command string and return it."""
    cmd = '/usr/sbin/gnmi_set '
    cmd += '-insecure -target_addr %s:%u ' % (env.gnmi_ip, env.gnmi_port)
    cmd += '-username %s -password %s ' % (env.username, env.password)
    for delete in delete_list:
        cmd += '--delete ' + delete + ' '
    for update in update_list:
        cmd += '--update ' + update + ' '
    for replace in replace_list:
        cmd += '--replace ' + replace + ' '
    return cmd


def gnmi_set(env, delete_list, update_list, replace_list, skip_cleanup=False):
    """
    Send GNMI set request with GNMI client.

    Automatically splits into multiple subprocess calls if the combined
    command line would exceed _MAX_CMD_BYTES (avoids OS ARG_MAX errors).

    Args:
        env: GNMIEnvironment
        delete_list: list for delete operations
        update_list: list for update operations
        replace_list: list for replace operations
        skip_cleanup: if True, caller handles temp file cleanup

    Returns:
    """
    total_ops = len(delete_list) + len(update_list) + len(replace_list)

    # Estimate total command size to decide whether to split.
    t0 = time.time()
    trial_cmd = _build_gnmi_set_cmd(env, delete_list, update_list, replace_list)
    _record("cmd_build", time.time() - t0)

    if len(trial_cmd) <= _MAX_CMD_BYTES:
        # Normal path — fits in one call.
        logging.info("TIMING: gnmi_set cmd length = %d chars, %d del + %d upd + %d rep ops",
                     len(trial_cmd), len(delete_list), len(update_list), len(replace_list))
        t0 = time.time()
        result = exec_cmd(trial_cmd)
        elapsed_subprocess = time.time() - t0
        _record("gnmi_set_subprocess", elapsed_subprocess)
        logging.info("TIMING: gnmi_set subprocess took %.3f s (rc=%d)",
                     elapsed_subprocess, result.returncode)
        if result.returncode == 0:
            logging.info("Command executed successfully")
    else:
        # Command too long — split into sub-batches.
        # Figure out how many ops fit per call by estimating avg arg size.
        avg_arg_len = len(trial_cmd) / max(total_ops, 1)
        base_cmd_len = len(_build_gnmi_set_cmd(env, [], [], []))
        ops_per_call = max(1, int((_MAX_CMD_BYTES - base_cmd_len) / avg_arg_len))
        logging.info("TIMING: cmd too long (%d bytes, %d ops) — splitting into sub-batches of ~%d ops",
                     len(trial_cmd), total_ops, ops_per_call)

        # Concatenate all ops with their type tag so we can chunk uniformly.
        tagged = ([('d', d) for d in delete_list] +
                  [('u', u) for u in update_list] +
                  [('r', r) for r in replace_list])

        sub_idx = 0
        for start in range(0, len(tagged), ops_per_call):
            sub_idx += 1
            chunk = tagged[start:start + ops_per_call]
            d = [path for tag, path in chunk if tag == 'd']
            u = [path for tag, path in chunk if tag == 'u']
            r = [path for tag, path in chunk if tag == 'r']

            t0 = time.time()
            cmd = _build_gnmi_set_cmd(env, d, u, r)
            _record("cmd_build", time.time() - t0)

            logging.info("TIMING: sub-batch %d — cmd %d chars, %d del + %d upd + %d rep",
                         sub_idx, len(cmd), len(d), len(u), len(r))
            t0 = time.time()
            result = exec_cmd(cmd)
            elapsed = time.time() - t0
            _record("gnmi_set_subprocess", elapsed)
            logging.info("TIMING: sub-batch %d took %.3f s (rc=%d)",
                         sub_idx, elapsed, result.returncode)
            if result.returncode != 0:
                logging.error("sub-batch %d failed (rc=%d)", sub_idx, result.returncode)

        logging.info("TIMING: gnmi_set split into %d sub-batches for %d total ops",
                     sub_idx, total_ops)

    if not skip_cleanup:
        # Cleanup the proto files created for update and replace
        t0 = time.time()
        cleanup_proto_files(update_list, work_dir=env.work_dir)
        cleanup_proto_files(replace_list)
        _record("proto_cleanup", time.time() - t0)

    return


def gnmi_get(env, path_list):
    """
    Send GNMI get request with GNMI client

    Args:
        env: GNMIEnvironment
        path_list: list for get path

    Returns:
        msg_list: list for get result
    """
    base_cmd = '/usr/sbin/gnmi_get '
    base_cmd += '-insecure -target_addr %s:%u ' % (env.gnmi_ip, env.gnmi_port)
    base_cmd += '-username %s -password %s -alsologtostderr -encoding PROTO ' % (env.username, env.password)

    for index, path in enumerate(path_list):
        cmd = base_cmd
        cmd += "-xpath "
        cmd += path
        cmd += " "
        cmd += "-proto_file "
        cmd += "get_result"

        result = exec_cmd(cmd)

        elem = path.split('/')
        if elem[3].startswith('_'):
            tblname = elem[3][1:]
        else:
            tblname = elem[3]

        print("-"*25)
        print(path)

        if result.returncode:
            error = "rpc error:"
            if error in result.stderr:
                rpc_error = result.stderr.split(error, 1)
                print("GRPC error: " + rpc_error[1])
            else:
                print("command failed: " + result.stderr)
            continue

        with open("get_result", 'rb') as file:
            # Read the entire content of the binary file
            binary_data = file.read()
            pb_obj = proto_utils.from_pb(tblname, binary_data)

        print(pb_obj)
        os.unlink("get_result")


def _send_batch(env, batch_num, delete_list, update_list, replace_list, batch_work_dir):
    """Send one batch via gnmi_set and clean up its temp dir. Runs in background thread."""
    logging.info("TIMING: batch %d — sending gnmi_set with %d del, %d upd, %d rep",
                 batch_num, len(delete_list), len(update_list), len(replace_list))
    gnmi_set(env, delete_list, update_list, replace_list, skip_cleanup=True)
    t0 = time.time()
    shutil.rmtree(batch_work_dir, ignore_errors=True)
    _record("proto_cleanup", time.time() - t0)


def process_template_chunk(res, env, dest_path, batch_val, sleep_secs):

    get_list = []
    delete_list = []
    update_list = []
    replace_list = []
    update_cnt = 0
    base_path = "/sonic-db:DPU_APPL_DB"
    base_path = "%s/dpu%d" % (base_path, env.dpu_index)
    batch_cnt = 0
    batch_num = 0

    # ── Pipeline: background send thread ──
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    pending_future = None

    # First batch writes to its own subdirectory
    batch_work_dir = os.path.join(env.work_dir, "b1") + "/"
    os.makedirs(batch_work_dir, exist_ok=True)

    logging.info("TIMING: processing %d operations, batch_val=%d", len(res), batch_val)

    for operation in res:
        batch_cnt += 1
        if operation["OP"] == "SET" or operation["OP"] == "REP":
            for k, v in operation.items():
                if k == "OP":
                    continue
                logging.debug("Config Json %s" % k)
                update_cnt += 1
                filename = "update%u" % update_cnt
                if proto_utils.ENABLE_PROTO:
                    t0 = time.time()
                    message = proto_utils.json_to_proto(k, v)
                    _record("proto_serialize", time.time() - t0)

                    t0 = time.time()
                    with open(batch_work_dir+filename, "wb") as file:
                        file.write(message)
                    _record("proto_file_write", time.time() - t0)
                else:
                    t0 = time.time()
                    text = json.dumps(v)
                    with open(batch_work_dir+filename, "w") as file:
                        file.write(text)
                    _record("proto_file_write", time.time() - t0)
                keys = k.split(":", 1)
                k = keys[0] + "[key=" + keys[1] + "]"
                if proto_utils.ENABLE_PROTO:
                    path = "%s/%s:$%s" % (base_path, k, batch_work_dir+filename)
                else:
                    path = "%s/%s:@%s" % (base_path, k, batch_work_dir+filename)
                if operation["OP"] == "REP":
                    replace_list.append(path)
                else:
                    update_list.append(path)
        elif operation["OP"] == "DEL":
            for k, v in operation.items():
                if k == "OP":
                    continue
                keys = k.split(":", 1)
                k = keys[0] + "[key=" + keys[1] + "]"
                path = "%s/%s" % (base_path, k)
                delete_list.append(path)
        elif operation["OP"] == "GET":
            for k, v in operation.items():
                if k == "OP":
                    continue
                if ":" not in k:
                    continue
                keys = k.split(":", 1)
                k = keys[0] + "[key=" + keys[1] + "]"
                path = "%s/%s" % (base_path, k)
                get_list.append(path)
        else:
            logging.error("Invalid operation %s" % operation["OP"])
            batch_cnt -= 1

        if batch_cnt == batch_val:
            batch_num += 1
            if sleep_secs:
                t0 = time.time()
                time.sleep(sleep_secs)
                _record("sleep", time.time() - t0)
            if get_list:
                gnmi_get(env, get_list)
            if delete_list or update_list or replace_list:
                # Wait for previous batch to finish before submitting next
                if pending_future is not None:
                    t0 = time.time()
                    pending_future.result()      # raises on error
                    _record("pipeline_wait", time.time() - t0)

                # Submit this batch to background thread
                pending_future = executor.submit(
                    _send_batch, env, batch_num,
                    delete_list, update_list, replace_list,
                    batch_work_dir,
                )

            # Reset for next batch — new lists, new subdir
            batch_cnt = 0
            update_cnt = 0
            delete_list = []
            update_list = []
            replace_list = []
            get_list = []
            batch_work_dir = os.path.join(env.work_dir, "b%d" % (batch_num + 1)) + "/"
            os.makedirs(batch_work_dir, exist_ok=True)

    # Wait for last pipelined batch
    if pending_future is not None:
        t0 = time.time()
        pending_future.result()
        _record("pipeline_wait", time.time() - t0)

    # Final partial batch (runs synchronously)
    if get_list:
        gnmi_get(env, get_list)
    if delete_list or update_list or replace_list:
        batch_num += 1
        logging.info("TIMING: batch %d (final) — sending gnmi_set with %d del, %d upd, %d rep",
                     batch_num, len(delete_list), len(update_list), len(replace_list))
        gnmi_set(env, delete_list, update_list, replace_list)

    executor.shutdown(wait=False)
    logging.info("TIMING: total batches sent: %d", batch_num)


def apply_gnmi_file(env, dest_path, batch_val=10, sleep_secs=0):
    """
    Apply dash configuration with gnmi client

    Args:
        env: GNMIEnvironment
        dest_path: configuration file path
        batch_val: how many commands in one batch
        sleep_secs: how many seconds to sleep between sending a batch and next

    Returns:
    """
    # Reset per-file totals
    _phase_totals.clear()

    t_file_start = time.time()

    os.makedirs(env.work_dir, exist_ok=True)

    t0 = time.time()
    with open(dest_path, 'r') as file:
        res = json.load(file)
    _record("json_load", time.time() - t0)
    logging.info("TIMING: json_load took %.3f s for %s", _phase_totals["json_load"], dest_path)

    if isinstance(res[0], dict):
        process_template_chunk(res, env, dest_path, batch_val, sleep_secs)
    else:
        for i in res:
            process_template_chunk(i, env, dest_path, batch_val, sleep_secs)
            time.sleep(TIME_BETWEEN_CHUNKS)

    total_wall = time.time() - t_file_start
    logging.info("TIMING: apply_gnmi_file total wall time: %.3f s", total_wall)
    _log_timing_summary()
