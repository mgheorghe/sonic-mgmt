#!/bin/bash
# Manual 1-ENI load on the OTHER Cisco SmartSwitch (MtFuji @ 10.36.77.120),
# DPU0 (000apl + 000eni + 000map). 10.36.77.121 / MtFujiv2 has all 8 DPUs
# hardware-stuck (PCIe non-enumeration); 10.36.77.120 / MtFuji has DPU0,
# DPU1, DPU4 Online, so we test against its DPU0.

set -o pipefail

NPU_IP=10.36.77.120
DPU_IDX=0
SSH_PORT=5021

CTR=eni_load_cisco_dpu0
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
echo "=== [1/3] APL (pl_100.dpu0.000apl.json) ==="; push_one apl pl_100.dpu0.000apl.json
echo "=== [2/3] ENI (pl_100.dpu0.000eni.json) ==="; push_one eni pl_100.dpu0.000eni.json
echo "=== [3/3] MAP (pl_100.dpu0.000map.json) ==="; push_one map pl_100.dpu0.000map.json
T1=$(date +%s.%N)
awk -v a="$T0" -v b="$T1" 'BEGIN { printf "WALL[TOTAL 1-ENI LOAD on Cisco DPU0]: %.3fs\n", (b - a) }' < /dev/null

docker rm -f $CTR >/dev/null
