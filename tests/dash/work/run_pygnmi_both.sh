#!/bin/bash
# Run the OPTIMIZED extracted gnmi_agent (pygnmi-based, persistent gRPC channel,
# no subprocess fork per batch) directly inside the sonic-mgmt container.
# Push apl + 000eni + 000map on both Nvidia DPU0 and Cisco MtFuji DPU0,
# capture per-file wall time.

set -o pipefail
cd /home/dash/sonic-mgmt/sonic-mgmt/tests/dash/gnmi_agent_extracted
export PYTHONPATH=.

push() {
  local label="$1" host="$2" port="$3" dpu="$4" f="$5"
  local t0=$(date +%s.%N)
  python3 gnmi_client.py --batch_val 1000 -l info \
    -t ${host}:${port} -i $dpu -n 8 \
    update -f /home/dash/sonic-mgmt/sonic-mgmt/tests/dash/configs/pl_100/dpu${dpu}/$f 2>&1 \
    | grep -E 'TIMINGS|elapsed:|module_imports|json_load|jinja|grpc|set_send|set_serialize|set_finalize|set_total|TOTAL|wait' \
    | tail -20
  local t1=$(date +%s.%N)
  awk -v a="$t0" -v b="$t1" -v lbl="$label" 'BEGIN { printf "WALL[%s]: %.3fs\n\n", lbl, (b - a) }' < /dev/null
}

run_platform() {
  local plat="$1" host="$2" port="$3" sshport="$4"
  echo "######################  $plat ($host:$port, dpu0)  ######################"
  # Flush so we measure a clean push
  sshpass -p password ssh -o StrictHostKeyChecking=no admin@$host \
    'sudo docker exec databasedpu0 sonic-db-cli DPU_APPL_DB FLUSHDB' >/dev/null 2>&1

  T0=$(date +%s.%N)
  push "${plat}-apl" $host $port 0 pl_100.dpu0.000apl.json
  push "${plat}-eni" $host $port 0 pl_100.dpu0.000eni.json
  push "${plat}-map" $host $port 0 pl_100.dpu0.000map.json
  T1=$(date +%s.%N)
  awk -v a="$T0" -v b="$T1" -v lbl="$plat" \
    'BEGIN { printf "===== WALL[%s TOTAL 1-ENI]: %.3fs =====\n\n", lbl, (b - a) }' < /dev/null
  echo "DBSIZE on databasedpu0:"
  sshpass -p password ssh -o StrictHostKeyChecking=no admin@$host \
    'sudo docker exec databasedpu0 sonic-db-cli DPU_APPL_DB DBSIZE'
  echo
}

run_platform NVIDIA 10.36.78.150 50052 5021
run_platform CISCO  10.36.77.120 50051 5021
