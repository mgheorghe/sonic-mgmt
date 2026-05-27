#!/bin/bash
# Manual 1-ENI load on Nvidia DPU1 via the persistent test container.
# Bypasses the pytest framework (which intermittently fails the gnmi pre-check
# race) and uses the same optimized gnmi-agent stack — mounted gnmi_client.py +
# go_gnmi_utils.py + proto_utils.py with GNMI_NOTLS=1, --no-proto, batch_val=1000.

set -o pipefail

NPU_IP=10.36.78.150
DPU_IDX=1
CONFIG_DIR=/dpu  # bind-mounted into the container

CTR=eni_load_nvidia
docker rm -f $CTR 2>/dev/null

docker run -d --name $CTR --network host --shm-size=256m -e GNMI_NOTLS=1 \
  --mount src=/home/dash/sonic-mgmt/gnmi/gnmi_client.py,target=/usr/sbin/gnmi_client.py,type=bind,readonly \
  --mount src=/home/dash/sonic-mgmt/gnmi/gnmi_agent/go_gnmi_utils.py,target=/usr/lib/python3/dist-packages/gnmi_agent/go_gnmi_utils.py,type=bind,readonly \
  --mount src=/home/dash/sonic-mgmt/gnmi/gnmi_agent/proto_utils.py,target=/usr/lib/python3/dist-packages/gnmi_agent/proto_utils.py,type=bind,readonly \
  --mount src=/home/dash/sonic-mgmt/tests/dash/configs/pl_100/dpu${DPU_IDX},target=/dpu,type=bind,readonly \
  sonic-gnmi-agent:2026march13 -c 'sleep infinity' >/dev/null
sleep 1

push_one() {
  local label="$1"; local f="$2"
  local t0=$(date +%s.%N)
  docker exec $CTR gnmi_client.py --batch_val 1000 --no-proto \
    -i $DPU_IDX -n 8 -t ${NPU_IP}:50052 update -f /dpu/$f 2>&1 \
    | grep -E 'json_load|proto_serialize|proto_file_write|gnmi_set_subprocess|proto_cleanup|TOTAL accounted|apply_gnmi_file total wall|exec_action total' \
    | tail -10
  local t1=$(date +%s.%N)
  awk -v a="$t0" -v b="$t1" -v lbl="$label" 'BEGIN { printf "WALL[%s]: %.3fs\n", lbl, (b - a) }' < /dev/null
  echo
}

T0=$(date +%s.%N)
echo "=== [1/3] APL ==="; push_one apl pl_100.dpu1.000apl.json
echo "=== [2/3] ENI ==="; push_one eni pl_100.dpu1.032eni.json
echo "=== [3/3] MAP ==="; push_one map pl_100.dpu1.032map.json
T1=$(date +%s.%N)
awk -v a="$T0" -v b="$T1" 'BEGIN { printf "WALL[TOTAL 1-ENI LOAD]: %.3fs\n", (b - a) }' < /dev/null

echo
echo "=== HGETALL COUNTERS_ENI_NAME_MAP on DPU1 ==="
sshpass -p password ssh -o StrictHostKeyChecking=no -p 5022 admin@$NPU_IP \
  'sudo sonic-db-cli COUNTERS_DB HGETALL COUNTERS_ENI_NAME_MAP' 2>&1 | tail -3

echo
echo "=== DPU_APPL_DB DBSIZE on DPU1 ==="
sshpass -p password ssh -o StrictHostKeyChecking=no -p 5022 admin@$NPU_IP \
  'sudo sonic-db-cli DPU_APPL_DB DBSIZE' 2>&1 | tail -3

docker rm -f $CTR >/dev/null
