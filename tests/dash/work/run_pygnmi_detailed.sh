#!/bin/bash
# Run the pygnmi-based agent and capture detailed timing on:
#   - client side (gnmi_client.py TIMINGS + wall clock)
#   - NPU side: databasedpu0 DBSIZE samples at 200ms intervals during the push
#     (shows HSET rate from gnmi-native into the per-DPU APPL_DB)
#   - DPU side: COUNTERS_ENI_NAME_MAP first-seen-time (when SAI completes)

set -o pipefail
cd /home/dash/sonic-mgmt/sonic-mgmt/tests/dash/gnmi_agent_extracted
export PYTHONPATH=.

CFG=/home/dash/sonic-mgmt/sonic-mgmt/tests/dash/configs/pl_100

sample_dbsize() {
  # Continuously sample databasedpu0 DBSIZE on the NPU.
  local host="$1"; local outfile="$2"
  : > "$outfile"
  while :; do
    local t=$(date +%s.%N)
    local n=$(sshpass -p password ssh -o StrictHostKeyChecking=no -o ConnectTimeout=2 admin@$host \
      'sudo docker exec databasedpu0 sonic-db-cli DPU_APPL_DB DBSIZE' 2>/dev/null | tr -d '\r' | tail -1)
    printf '%s %s\n' "$t" "${n:-?}" >> "$outfile"
    sleep 0.2
  done
}

sample_eni_counters() {
  # Continuously sample COUNTERS_ENI_NAME_MAP count on the DPU.
  local host="$1"; local sshport="$2"; local outfile="$3"
  : > "$outfile"
  while :; do
    local t=$(date +%s.%N)
    local n=$(sshpass -p password ssh -o StrictHostKeyChecking=no -o ConnectTimeout=2 -p $sshport admin@$host \
      'sudo sonic-db-cli COUNTERS_DB HLEN COUNTERS_ENI_NAME_MAP' 2>/dev/null | tr -d '\r' | tail -1)
    printf '%s %s\n' "$t" "${n:-?}" >> "$outfile"
    sleep 0.2
  done
}

run_platform() {
  local plat="$1" host="$2" port="$3" sshport="$4"
  local outdir="/tmp/perf_run4_$plat"
  rm -rf $outdir; mkdir -p $outdir

  echo "############### $plat ($host:$port, dpu0) ###############"

  # Pre-state: flush, get baseline counter
  sshpass -p password ssh -o StrictHostKeyChecking=no admin@$host \
    'sudo docker exec databasedpu0 sonic-db-cli DPU_APPL_DB FLUSHDB' >/dev/null 2>&1
  local pre_eni=$(sshpass -p password ssh -o StrictHostKeyChecking=no -p $sshport admin@$host \
    'sudo sonic-db-cli COUNTERS_DB HLEN COUNTERS_ENI_NAME_MAP' 2>/dev/null | tr -d '\r' | tail -1)
  echo "Pre-push ENI count on DPU0: $pre_eni"

  # Start samplers
  sample_dbsize    $host          $outdir/dbsize.log   &
  local PID_DB=$!
  sample_eni_counters $host $sshport $outdir/eni.log &
  local PID_ENI=$!

  # Run pushes
  local T0=$(date +%s.%N)
  for f in 000apl 000eni 000map; do
    local t0=$(date +%s.%N)
    python3 gnmi_client.py --batch_val 3000 -l info \
      -t ${host}:${port} -i 0 -n 8 \
      update -f $CFG/dpu0/pl_100.dpu0.${f}.json 2>&1 \
      | tee $outdir/client_${f}.log \
      | grep -E '^elapsed:|TIMINGS|module_imports|json_load' | tail -5
    local t1=$(date +%s.%N)
    awk -v a="$t0" -v b="$t1" -v ff="$f" \
      'BEGIN { printf "  WALL[%s] %.3fs\n", ff, (b - a) }' < /dev/null
  done
  local T1=$(date +%s.%N)
  echo "$T0 push_start" > $outdir/marks.log
  echo "$T1 push_end"  >> $outdir/marks.log
  awk -v a="$T0" -v b="$T1" -v p="$plat" \
    'BEGIN { printf "  TOTAL[%s] %.3fs\n", p, (b - a) }' < /dev/null

  # Let SAI catch up for a few seconds, then stop samplers
  sleep 5
  kill $PID_DB $PID_ENI 2>/dev/null
  wait $PID_DB $PID_ENI 2>/dev/null

  # Post-state
  local post_eni=$(sshpass -p password ssh -o StrictHostKeyChecking=no -p $sshport admin@$host \
    'sudo sonic-db-cli COUNTERS_DB HLEN COUNTERS_ENI_NAME_MAP' 2>/dev/null | tr -d '\r' | tail -1)
  local dbsize=$(sshpass -p password ssh -o StrictHostKeyChecking=no admin@$host \
    'sudo docker exec databasedpu0 sonic-db-cli DPU_APPL_DB DBSIZE' 2>/dev/null | tr -d '\r' | tail -1)
  echo "Post-push: databasedpu0 DBSIZE=$dbsize, DPU0 ENI_NAME_MAP count=$post_eni"
  echo "Samples: dbsize.log=$(wc -l < $outdir/dbsize.log) eni.log=$(wc -l < $outdir/eni.log)"
  echo "Output dir: $outdir"
  echo
}

run_platform NVIDIA 10.36.78.150 50052 5021
run_platform CISCO  10.36.77.120 50051 5021
