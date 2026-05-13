import json
import logging
import os
import time

import grpc
import proto_utils
from pygnmi.spec.v080 import gnmi_pb2, gnmi_pb2_grpc


TIME_BETWEEN_CHUNKS = 1


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


def gnmi_set(env, delete_list, update_list, replace_list):
    """
    Send a single GNMI SetRequest over gRPC. Replaces the previous
    /usr/sbin/gnmi_set shell-out, which had a command-line length cap
    that broke large config batches.

    Args:
        env: GNMIEnvironment
        delete_list: list of gnmi-cli style path strings for delete ops
        update_list: list of "<path>:$<file>" strings (file holds proto bytes)
        replace_list: same shape as update_list, for replace ops
    """
    if not (delete_list or update_list or replace_list):
        return

    req = gnmi_pb2.SetRequest()
    for d in delete_list:
        p, _ = _parse_gnmi_cli_path(d)
        req.delete.append(p)
        logging.info("Deleting " + d)
    for u in update_list:
        p, fp = _parse_gnmi_cli_path(u)
        if fp is None:
            logging.error("Update path missing :$<file>: %s", u)
            continue
        upd = req.update.add()
        upd.path.CopyFrom(p)
        upd.val.proto_bytes = _read_file_bytes(fp)
    for r in replace_list:
        p, fp = _parse_gnmi_cli_path(r)
        if fp is None:
            logging.error("Replace path missing :$<file>: %s", r)
            continue
        rep = req.replace.add()
        rep.path.CopyFrom(p)
        rep.val.any_val.value = _read_file_bytes(fp)
        logging.info("Replacing " + r)

    try:
        with _open_channel(env) as channel:
            stub = gnmi_pb2_grpc.gNMIStub(channel)
            response = stub.Set(req, metadata=_auth_metadata(env))
            logging.debug("SetResponse: %s", response)
            logging.info("Command executed successfully")
    except grpc.RpcError as e:
        logging.error("gNMI Set failed: %s", e)
        raise

    cleanup_proto_files(update_list)
    cleanup_proto_files(replace_list)


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


def process_template_chunk(res, env, dest_path, batch_val, sleep_secs):

    get_list = []
    delete_list = []
    update_list = []
    replace_list = []
    update_cnt = 0
    base_path = "/sonic-db:DPU_APPL_DB"
    base_path = "%s/dpu%d" % (base_path, env.dpu_index)
    batch_cnt = 0

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
                    message = proto_utils.json_to_proto(k, v)
                    with open(env.work_dir+filename, "wb") as file:
                        file.write(message)
                else:
                    text = json.dumps(v)
                    with open(env.work_dir+filename, "w") as file:
                        file.write(text)
                keys = k.split(":", 1)
                k = keys[0] + "[key=" + keys[1] + "]"
                if proto_utils.ENABLE_PROTO:
                    path = "%s/%s:$%s" % (base_path, k, env.work_dir+filename)
                else:
                    path = "%s/%s:@%s" % (base_path, k, env.work_dir+filename)
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
            time.sleep(sleep_secs)
            if get_list:
                gnmi_get(env, get_list)
            if delete_list or update_list or replace_list:
                gnmi_set(env, delete_list, update_list, replace_list)
            batch_cnt = 0
            update_cnt = 0
            delete_list = []
            update_list = []
            replace_list = []
            get_list = []

    if get_list:
        gnmi_get(env, get_list)
    if delete_list or update_list or replace_list:
        gnmi_set(env, delete_list, update_list, replace_list)


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
    with open(dest_path, 'r') as file:
        res = json.load(file)

    if isinstance(res[0], dict):
        process_template_chunk(res, env, dest_path, batch_val, sleep_secs)
    else:
        for i in res:
            process_template_chunk(i, env, dest_path, batch_val, sleep_secs)
            time.sleep(TIME_BETWEEN_CHUNKS)
