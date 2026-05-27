#!/usr/bin/env python3
"""Apply all patches to test_dash_api_speed_pl.py for the optimized run."""
import sys

PATH = '/home/dash/sonic-mgmt/sonic-mgmt/tests/dash/test_dash_api_speed_pl.py'

with open(PATH) as f:
    src = f.read()

orig = src

# Patch 1: _ENI_COUNT = "1"
src = src.replace('_ENI_COUNT = "ALL"', '_ENI_COUNT = "1"')

# Patch 2: filter — _ENI_COUNT N = N ENIs (apl + first N eni/map pairs)
old_filter = '''        n = int(_ENI_COUNT)
        filtered = []
        for f in files:
            m = re.search(r"\\.(\\d{3})(apl|eni|map)\\.json$", f)
            if m and int(m.group(1)) < n:
                filtered.append(f)
        assert filtered, f"_ENI_COUNT={_ENI_COUNT} filtered out all files (had {len(files)})"'''

new_filter = '''        n = int(_ENI_COUNT)
        # n = number of ENIs. Keep apl files unconditionally + first N (eni,map) pairs.
        eni_indices = sorted({
            int(re.search(r"\\.(\\d{3})(eni|map)\\.json$", f).group(1))
            for f in files
            if re.search(r"\\.\\d{3}(eni|map)\\.json$", f)
        })
        keep_indices = set(eni_indices[:n])
        filtered = []
        for f in files:
            m = re.search(r"\\.(\\d{3})(apl|eni|map)\\.json$", f)
            if not m:
                continue
            kind = m.group(2)
            idx = int(m.group(1))
            if kind == 'apl' or idx in keep_indices:
                filtered.append(f)
        assert filtered, f"_ENI_COUNT={_ENI_COUNT} filtered out all files (had {len(files)})"'''

assert old_filter in src, 'filter old block missing'
src = src.replace(old_filter, new_filter)

# Patch 3: also bind-mount proto_utils.py AND gnmi_client.py into the container
old_mount = '''    repo_go_utils = os.path.abspath(os.path.join(
        os.path.dirname(__file__), "..", "..", "gnmi", "gnmi_agent", "go_gnmi_utils.py"
    ))
    go_utils_host = _container_path_to_host(repo_go_utils)
    go_utils_mount = (
        f" --mount src={go_utils_host},"  # noqa: E231
        f"target=/usr/lib/python3/dist-packages/gnmi_agent/go_gnmi_utils.py,type=bind,readonly"  # noqa: E231
    )'''

new_mount = '''    repo_go_utils = os.path.abspath(os.path.join(
        os.path.dirname(__file__), "..", "..", "gnmi", "gnmi_agent", "go_gnmi_utils.py"
    ))
    go_utils_host = _container_path_to_host(repo_go_utils)
    repo_proto_utils = os.path.abspath(os.path.join(
        os.path.dirname(__file__), "..", "..", "gnmi", "gnmi_agent", "proto_utils.py"
    ))
    proto_utils_host = _container_path_to_host(repo_proto_utils)
    repo_gnmi_client = os.path.abspath(os.path.join(
        os.path.dirname(__file__), "..", "..", "gnmi", "gnmi_client.py"
    ))
    gnmi_client_host = _container_path_to_host(repo_gnmi_client)
    go_utils_mount = (
        f" --mount src={go_utils_host},"  # noqa: E231
        f"target=/usr/lib/python3/dist-packages/gnmi_agent/go_gnmi_utils.py,type=bind,readonly"  # noqa: E231
        f" --mount src={proto_utils_host},"  # noqa: E231
        f"target=/usr/lib/python3/dist-packages/gnmi_agent/proto_utils.py,type=bind,readonly"  # noqa: E231
        f" --mount src={gnmi_client_host},"  # noqa: E231
        f"target=/usr/sbin/gnmi_client.py,type=bind,readonly"  # noqa: E231
    )'''

assert old_mount in src, 'mount old block missing'
src = src.replace(old_mount, new_mount)

# Patch 4: env_opts adds GNMI_NOTLS=1 for non-TLS servers
old_env = '''    env_opts = ""
    if server_mode in ("tls", "mtls") and cert_mount_opt:'''
new_env = '''    env_opts = ""
    if server_mode not in ("tls", "mtls"):
        env_opts += " -e GNMI_NOTLS=1"
    if server_mode in ("tls", "mtls") and cert_mount_opt:'''
assert old_env in src, 'env_opts block missing'
src = src.replace(old_env, new_env)

# Patch 5: gnmi_client.py invocation: drop --no-proto, batch_val=1000
old_cmd = 'gnmi_client.py --batch_val 10000 --no-proto -i {dpu_index}'
new_cmd = 'gnmi_client.py --batch_val 1000 -i {dpu_index}'
assert old_cmd in src, 'cmd line missing'
src = src.replace(old_cmd, new_cmd)

if src == orig:
    print('NO CHANGES')
    sys.exit(1)

with open(PATH, 'w') as f:
    f.write(src)
print('ALL PATCHES APPLIED')
