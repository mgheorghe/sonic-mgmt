import json
import logging
import os
import queue
import threading
import time

import grpc
import proto_utils
from pygnmi.spec.v080 import gnmi_pb2, gnmi_pb2_grpc


TIME_BETWEEN_CHUNKS = 1

# name -> [call_count, total_seconds]. Filled in by the `phase` context manager;
# rendered by dump_timings() to break down where wall-clock time is spent.
TIMINGS = {}


class phase:
    """Context manager that accumulates wall-clock time per named phase."""

    def __init__(self, name):
        self.name = name

    def __enter__(self):
        self._start = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        entry = TIMINGS.setdefault(self.name, [0, 0.0])
        entry[0] += 1
        entry[1] += time.perf_counter() - self._start
        return False


def dump_timings():
    if not TIMINGS:
        return
    name_w = max(len(n) for n in TIMINGS)
    print("phase breakdown (sorted by total time):")
    for name, (count, total) in sorted(TIMINGS.items(), key=lambda kv: -kv[1][1]):
        avg = total / count if count else 0.0
        print("  %-*s %5d x  total %8.3fs  avg %.4fs" %
              (name_w, name, count, total, avg))


class GNMIEnvironment:
    gnmi_ip = "127.0.0.1"
    gnmi_port = 8080
    work_dir = "/"
    username = "admin"
    password = "password"
    dpu_index = 0
    num_dpus = 1


def cleanup_proto_files(cmd_list):
    if not cmd_list:
        return
    for cmd in cmd_list:
        if ":$" not in cmd:
            continue
        del_file = cmd.split(":$", 1)[1]
        if del_file and os.path.exists(del_file):
            logging.debug("Deleting file:" + del_file)
            os.unlink(del_file)


def _parse_gnmi_cli_path(path_str):
    """
    Parse a gnmi-cli style path. Two shapes are accepted:
        /<origin>:<elem1>[/<elem2>[k=v]]...            (used for delete/get)
        /<origin>:<elem1>[/<elem2>[k=v]]...:$<file>    (used for update/replace
                                                       with proto-encoded bytes
                                                       sourced from a file)
    Return (gnmi_pb2.Path, filepath_or_None).
    """
    filepath = None
    if ":$" in path_str:
        path_str, filepath = path_str.split(":$", 1)
    p = path_str[1:] if path_str.startswith("/") else path_str
    origin = None
    head, _, rest = p.partition("/")
    if ":" in head:
        origin, head = head.split(":", 1)
    elems = []
    raw_elems = ([head] if head else []) + (rest.split("/") if rest else [])
    for raw in raw_elems:
        if not raw:
            continue
        name, keys = raw, {}
        if "[" in raw and raw.endswith("]"):
            name, kv = raw[:-1].split("[", 1)
            k, _, v = kv.partition("=")
            keys[k] = v
        elems.append(gnmi_pb2.PathElem(name=name, key=keys))
    return gnmi_pb2.Path(origin=origin, elem=elems), filepath


def _read_file_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def _open_channel(env):
    """
    Open a gRPC channel to the gNMI server. If any of the GNMI_CA /
    GNMI_CLIENT_CERT / GNMI_CLIENT_KEY env vars are set, use TLS; the
    optional GNMI_TARGET_NAME overrides the server name for SAN checks.
    Otherwise fall back to an insecure channel.
    """
    target = "%s:%d" % (env.gnmi_ip, env.gnmi_port)
    ca = os.environ.get("GNMI_CA")
    cert = os.environ.get("GNMI_CLIENT_CERT")
    key = os.environ.get("GNMI_CLIENT_KEY")
    target_name = os.environ.get("GNMI_TARGET_NAME")
    if ca or cert or key:
        creds = grpc.ssl_channel_credentials(
            root_certificates=_read_file_bytes(ca) if ca else None,
            private_key=_read_file_bytes(key) if key else None,
            certificate_chain=_read_file_bytes(cert) if cert else None,
        )
        options = []
        if target_name:
            options.append(("grpc.ssl_target_name_override", target_name))
        logging.debug("Opening TLS channel to %s (target_name=%s)", target, target_name)
        return grpc.secure_channel(target, creds, options=options)
    logging.debug("Opening insecure channel to %s", target)
    return grpc.insecure_channel(target)


def _auth_metadata(env):
    return [("username", env.username), ("password", env.password)]


class GnmiSetSession:
    """
    Pipelined gNMI Set sender. SetRequests are built on the calling
    thread (where proto serialization already lives) and handed to a
    single background sender thread that does the stub.Set RPC. This
    overlaps the CPU-bound serialize/build phase with the network RPC,
    cutting wall-clock to ~max(build, rpc) * num_batches instead of
    (build + rpc) * num_batches.

    Also reuses one gRPC channel for the whole session so repeated
    submits skip the per-batch TCP/HTTP2 setup cost.

    Usage:
        with GnmiSetSession(env) as sess:
            for batch in batches:
                sess.submit(delete_list, update_list, replace_list)
    """

    def __init__(self, env, queue_depth=2):
        self.env = env
        self._channel = _open_channel(env)
        self._stub = gnmi_pb2_grpc.gNMIStub(self._channel)
        self._queue = queue.Queue(maxsize=queue_depth)
        self._exc = None
        self._thread = threading.Thread(target=self._sender_loop, daemon=True)
        self._thread.start()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    def submit(self, delete_list, update_list, replace_list):
        if not (delete_list or update_list or replace_list):
            return
        # Surface any failure from the sender thread before queuing more work.
        self._raise_if_failed()
        with phase("build_setrequest"):
            req = gnmi_pb2.SetRequest()
            for d in delete_list:
                req.delete.append(_coerce_delete(d))
            for u in update_list:
                item = _coerce_update(u)
                if item is None:
                    continue
                p, payload = item
                upd = req.update.add()
                upd.path.CopyFrom(p)
                upd.val.proto_bytes = payload
            for r in replace_list:
                item = _coerce_update(r)
                if item is None:
                    continue
                p, payload = item
                rep = req.replace.add()
                rep.path.CopyFrom(p)
                rep.val.proto_bytes = payload
        self._queue.put(req)

    def close(self):
        self._queue.put(None)  # sentinel: stop after draining
        self._thread.join()
        self._channel.close()
        self._raise_if_failed()

    def _raise_if_failed(self):
        if self._exc is not None:
            exc, self._exc = self._exc, None
            raise exc

    def _sender_loop(self):
        try:
            while True:
                req = self._queue.get()
                if req is None:
                    return
                with phase("rpc_set"):
                    response = self._stub.Set(req, metadata=_auth_metadata(self.env))
                logging.debug("SetResponse: %s", response)
                logging.info("Command executed successfully")
        except Exception as e:
            self._exc = e


def _coerce_delete(entry):
    """delete_list entry may be a gnmi-cli path string or a pre-built Path."""
    if isinstance(entry, gnmi_pb2.Path):
        return entry
    p, _ = _parse_gnmi_cli_path(entry)
    return p


def _coerce_update(entry):
    """
    update/replace_list entry may be either:
      - "<gnmi-cli-path>:$<file>" string (legacy; file holds proto bytes), or
      - (gnmi_pb2.Path, bytes) tuple (preferred; no temp file).
    Return (gnmi_pb2.Path, bytes) or None on parse failure.
    """
    if isinstance(entry, tuple):
        return entry
    p, fp = _parse_gnmi_cli_path(entry)
    if fp is None:
        logging.error("path missing :$<file>: %s", entry)
        return None
    with phase("read_proto_file"):
        return p, _read_file_bytes(fp)


def gnmi_set(env, delete_list, update_list, replace_list):
    """
    Send a single GNMI SetRequest over gRPC.

    Each list element may be either:
      - a gnmi-cli-style string (legacy path; for update/replace the
        proto bytes are read from the ":$<file>" suffix, file is
        deleted after the RPC succeeds), or
      - a gnmi_pb2.Path (for delete) / (gnmi_pb2.Path, bytes) tuple
        (for update/replace) -- avoids the temp-file round-trip and
        the xpath re-parse, which together dominate the build phase
        on large batches.
    """
    if not (delete_list or update_list or replace_list):
        return

    with phase("build_setrequest"):
        req = gnmi_pb2.SetRequest()
        for d in delete_list:
            req.delete.append(_coerce_delete(d))
        for u in update_list:
            item = _coerce_update(u)
            if item is None:
                continue
            p, payload = item
            upd = req.update.add()
            upd.path.CopyFrom(p)
            upd.val.proto_bytes = payload
        for r in replace_list:
            item = _coerce_update(r)
            if item is None:
                continue
            p, payload = item
            rep = req.replace.add()
            rep.path.CopyFrom(p)
            rep.val.proto_bytes = payload

    try:
        with phase("open_channel"):
            channel = _open_channel(env)
            stub = gnmi_pb2_grpc.gNMIStub(channel)
        try:
            with phase("rpc_set"):
                response = stub.Set(req, metadata=_auth_metadata(env))
            logging.debug("SetResponse: %s", response)
            logging.info("Command executed successfully")
        finally:
            channel.close()
    except grpc.RpcError as e:
        logging.error("gNMI Set failed: %s", e)
        raise

    str_updates = [x for x in update_list if isinstance(x, str)]
    str_replaces = [x for x in replace_list if isinstance(x, str)]
    if str_updates or str_replaces:
        with phase("cleanup_proto_files"):
            cleanup_proto_files(str_updates)
            cleanup_proto_files(str_replaces)


def gnmi_get(env, path_list):
    """
    Send GNMI GetRequest over gRPC for the given paths and print the
    decoded protobuf for each table.

    Args:
        env: GNMIEnvironment
        path_list: list of gnmi-cli style path strings

    Returns:
        None
    """
    if not path_list:
        return

    paths_pb = [_parse_gnmi_cli_path(p)[0] for p in path_list]
    req = gnmi_pb2.GetRequest(path=paths_pb, encoding=gnmi_pb2.PROTO)

    try:
        with _open_channel(env) as channel:
            stub = gnmi_pb2_grpc.gNMIStub(channel)
            response = stub.Get(req, metadata=_auth_metadata(env))
    except grpc.RpcError as e:
        for path in path_list:
            print("-" * 25)
            print(path)
            print("GRPC error: " + str(e))
        return

    for notif, in_path in zip(response.notification, path_list):
        print("-" * 25)
        print(in_path)
        elem = in_path.split("/")
        tblname = elem[3][1:] if len(elem) > 3 and elem[3].startswith("_") else (elem[3] if len(elem) > 3 else "")
        for update in notif.update:
            val = update.val
            if val.HasField("bytes_val"):
                payload = val.bytes_val
            elif val.HasField("proto_bytes"):
                payload = val.proto_bytes
            elif val.HasField("any_val"):
                payload = val.any_val.value
            else:
                print(val)
                continue
            try:
                pb_obj = proto_utils.from_pb(tblname, payload)
                print(pb_obj)
            except Exception as ex:
                logging.error("Failed to decode proto for %s: %s", tblname, ex)
                print(payload)


def _build_dpu_path(dpu_index, table_key_str):
    """
    Build a gnmi_pb2.Path for "/sonic-db:DPU_APPL_DB/dpu<N>/<TABLE>[key=<KEY>]"
    from a "TABLE:KEY..." string (splitting on the first colon only).
    """
    table, _, key = table_key_str.partition(":")
    return gnmi_pb2.Path(
        origin="sonic-db",
        elem=[
            gnmi_pb2.PathElem(name="DPU_APPL_DB"),
            gnmi_pb2.PathElem(name="dpu%d" % dpu_index),
            gnmi_pb2.PathElem(name=table, key={"key": key}),
        ],
    )


def process_template_chunk(res, env, dest_path, batch_val, sleep_secs, session=None):
    """
    Accumulate the operations in `res` into batches of `batch_val` and
    flush each batch as a gNMI Set. If `session` is provided, batched
    sets are pipelined through it (build on this thread, RPC on the
    sender thread); otherwise we fall back to the synchronous gnmi_set
    call (kept for direct callers).
    """
    def _flush_set(delete_list, update_list, replace_list):
        if session is not None:
            session.submit(delete_list, update_list, replace_list)
        else:
            gnmi_set(env, delete_list, update_list, replace_list)

    get_list = []
    delete_list = []
    update_list = []
    replace_list = []
    batch_cnt = 0

    for operation in res:
        batch_cnt += 1
        if operation["OP"] == "SET" or operation["OP"] == "REP":
            for k, v in operation.items():
                if k == "OP":
                    continue
                logging.debug("Config Json %s" % k)
                if proto_utils.ENABLE_PROTO:
                    with phase("proto_serialize"):
                        payload = proto_utils.json_to_proto(k, v)
                else:
                    with phase("json_serialize"):
                        payload = json.dumps(v).encode("utf-8")
                path_pb = _build_dpu_path(env.dpu_index, k)
                if operation["OP"] == "REP":
                    replace_list.append((path_pb, payload))
                else:
                    update_list.append((path_pb, payload))
        elif operation["OP"] == "DEL":
            for k, v in operation.items():
                if k == "OP":
                    continue
                delete_list.append(_build_dpu_path(env.dpu_index, k))
        elif operation["OP"] == "GET":
            for k, v in operation.items():
                if k == "OP":
                    continue
                if ":" not in k:
                    continue
                keys = k.split(":", 1)
                k_xpath = keys[0] + "[key=" + keys[1] + "]"
                get_list.append("/sonic-db:DPU_APPL_DB/dpu%d/%s" % (env.dpu_index, k_xpath))
        else:
            logging.error("Invalid operation %s" % operation["OP"])
            batch_cnt -= 1

        if batch_cnt == batch_val:
            if sleep_secs:
                with phase("batch_sleep"):
                    time.sleep(sleep_secs)
            if get_list:
                gnmi_get(env, get_list)
            _flush_set(delete_list, update_list, replace_list)
            batch_cnt = 0
            delete_list = []
            update_list = []
            replace_list = []
            get_list = []

    if get_list:
        gnmi_get(env, get_list)
    _flush_set(delete_list, update_list, replace_list)


def apply_gnmi_file(env, dest_path, batch_val=10, sleep_secs=0):
    """
    Apply dash configuration with gnmi client. All batched Sets are
    pipelined through a single GnmiSetSession so request build (CPU)
    overlaps with RPC (network), and one gRPC channel is reused for
    the whole file.

    Args:
        env: GNMIEnvironment
        dest_path: configuration file path
        batch_val: how many commands in one batch
        sleep_secs: how many seconds to sleep between sending a batch and next
    """
    with open(dest_path, 'r') as file:
        res = json.load(file)

    with GnmiSetSession(env) as session:
        if isinstance(res[0], dict):
            process_template_chunk(res, env, dest_path, batch_val, sleep_secs, session=session)
        else:
            for i in res:
                process_template_chunk(i, env, dest_path, batch_val, sleep_secs, session=session)
                time.sleep(TIME_BETWEEN_CHUNKS)
