# DASH API Speed Test — Setup & Run Guide

## Overview

`test_dash_api_speed_pl.py` measures the time to push `private-link-50` DASH configs
onto a DPU via gNMI, then verifies that 64 ENIs are correctly programmed in `COUNTERS_DB`.

**Branch:** `work-api-load-speed-test`

---

## Infrastructure

| Component | Host | Address | Credentials |
|-----------|------|---------|-------------|
| SMD test server | smd | 10.36.79.161 | dash / dash (root also available) |
| NPU | keysight-nss01 | 10.36.78.150 | admin / password |
| DPU0 midplane | — | 169.254.200.1 | reachable from NPU only |
| PTF container | ptf_keysight-nss01 | runs on SMD | — |
| sonic-mgmt container | sonic-mgmt | runs on SMD | — |

**Ansible vault password:** `password123`

---

## Step 1 — Start the sonic-mgmt container (SMD)

```bash
docker start sonic-mgmt
```

Verify it is running:

```bash
docker ps | grep sonic-mgmt
```

---

## Step 2 — Start the PTF container (SMD)

The PTF container does **not** auto-start on reboot. Check its status first:

```bash
docker ps | grep ptf
```

**If the container is stopped:**

```bash
docker start ptf_keysight-nss01
```

**If the container is gone entirely (first run or after wipe), recreate it:**

```bash
docker run -d \
  --name ptf_keysight-nss01 \
  --privileged \
  sonicdev-microsoft.azurecr.io:443/docker-ptf:latest
```

> **Note:** `testbed-cli.sh add-topo` does NOT work for this testbed. The `lab` Ansible
> inventory is missing `servers` and `vm_host` entries for `sonic-mgmt-keysight`, so all
> plays are skipped. Always use `docker run` / `docker start` directly.

---

## Step 2b — Fix PTF container networking (after reboot or first run)

The ansible inventory connects to the PTF container via `172.17.0.1:2222`. This requires
iptables NAT rules on the SMD host and an SSH key from sonic-mgmt injected into the PTF container.

Run all of the following **on SMD as root**:

```bash
# Verify PTF container IP (expected: 172.17.0.3)
docker inspect ptf_keysight-nss01 --format '{{json .NetworkSettings.Networks}}'

# Start SSH inside the PTF container
docker exec ptf_keysight-nss01 service ssh start

# Set up NAT: 172.17.0.1:2222 → PTF:22
iptables -t nat -A PREROUTING -p tcp -d 172.17.0.1 --dport 2222 -j DNAT --to-destination 172.17.0.3:22
iptables -t nat -A POSTROUTING -j MASQUERADE

# Inject sonic-mgmt SSH public key into PTF container (password auth does not work)
docker exec sonic-mgmt cat /root/.ssh/id_rsa.pub | \
  docker exec -i ptf_keysight-nss01 bash -c "mkdir -p /root/.ssh && cat >> /root/.ssh/authorized_keys && chmod 600 /root/.ssh/authorized_keys"
```

Verify connectivity from **inside the sonic-mgmt container**:

```bash
ssh -o StrictHostKeyChecking=no -p 2222 root@172.17.0.1 echo ok
```

> **Note:** These iptables rules are lost on reboot — re-apply them every time SMD restarts.
> Password auth to the PTF container does not work regardless of what password is tried;
> key injection is the only method that works.

---

## Step 3 — Verify DPU is alive (from NPU)

SSH to the NPU and ping the DPU midplane IP:

```bash
ssh admin@10.36.78.150
ping -c 2 169.254.200.1
```

> `show chassis module midplane-status` is **not reliable** after the test has run once —
> it will show `False` even when the DPU is alive. Use ping as the real health check.

---

## Step 4 — Run the test

Enter the sonic-mgmt container on SMD:

```bash

su dash
cd /home/dash/sonic-mgmt

docker exec -it sonic-mgmt bash
```

Then run the test:

NPU:
```
sudo sonic-dpu-mgmt-traffic.sh inbound -e --dpus all --ports 5021,5022,5023,5024
```


```bash
cd /home/dash/sonic-mgmt/sonic-mgmt/tests && \
  ANSIBLE_LIBRARY=/home/dash/sonic-mgmt/sonic-mgmt/ansible/library \
  ANSIBLE_MODULE_UTILS=/home/dash/sonic-mgmt/sonic-mgmt/ansible/module_utils \
  pytest dash/test_dash_api_speed_pl.py \
    --testbed=keysight-nss01 \
    --testbed_file=../ansible/testbed.yaml \
    --inventory=../ansible/lab \
    --host-pattern=keysight-nss01 \
    --dpu_index=0 \
    --dpu-pattern=keysight-nss01-dpu0 \
    --cache-clear -v
```

Change `--dpu_index` and `--dpu-pattern` to target a different DPU (0–3).

---

## DPU Reference

| DPU | Midplane IP    | Dataplane IP | Loopback0    |
|-----|----------------|--------------|--------------|
| 0   | 169.254.200.1  | 10.0.0.57    | 221.0.0.1/32 |
| 1   | 169.254.200.2  | 10.0.0.59    | 221.0.0.2/32 |
| 2   | 169.254.200.3  | 10.0.0.61    | 221.0.0.3/32 |
| 3   | 169.254.200.4  | 10.0.0.63    | 221.0.0.4/32 |

---

## What the test does

1. Pre-flight ping to DPU midplane IP (from NPU) to confirm DPU is alive.
2. Discovers JSON config files under `configs/private-link-50/dpu<N>/`.
3. Sets up DPU networking on each run:
   - Adds `Loopback0` IP on DPU.
   - Removes `default via 169.254.200.254` midplane routes from DPU.
   - Adds permanent static ARP entries on NPU for dataplane next-hops.
4. Collects memory baseline (NPU + DPU + Redis).
5. For each JSON file: copies to NPU `/tmp/dash_load/`, runs `docker run sonic-gnmi-agent`
   to push via gNMI, logs time per file.
6. Post-push liveness ping to DPU dataplane IP.
7. Polls `COUNTERS_DB HGETALL COUNTERS_ENI_NAME_MAP` on DPU until 64 ENIs appear.
8. Collects memory snapshot after and prints results.

---

## Key files

| File | Purpose |
|------|---------|
| `tests/dash/test_dash_api_speed_pl.py` | Main test |
| `tests/dash/gnmi_utils.py` | gNMI helpers |
| `tests/dash/proto_utils.py` | Protobuf serialization |
| `tests/dash/configs/private-link-50/dpu<N>/` | JSON config files (one per table type) |
