#!/bin/bash
# Cisco-only version of run_pygnmi_detailed.sh. Runs the pygnmi agent against
# Cisco MtFuji DPU0 (gnmi-native on :50051), with 200ms-cadence samplers on
# the NPU databasedpu0 DBSIZE and the DPU's COUNTERS_ENI_NAME_MAP HLEN.

set -o pipefail
cd /home/dash/sonic-mgmt/sonic-mgmt/tests/dash/gnmi_agent_extracted
export PYTHONPATH=.

CFG=/home/dash/sonic-mgmt/sonic-mgmt/tests/dash/configs/pl_100
PLAT=CISCO
HOST=10.36.77.120
PORT=50051
SSHPORT=5021
OUT=/tmp/perf_run5_$PLAT
rm -rf $OUT; mkdir -p $OUT

echo "############### $PLAT ($HOST:$PORT, dpu0) ###############"

# Flush so we measure a clean push
sshpass -p password ssh -o StrictHostKeyChecking=no admin@$HOST \
  'sudo docker exec databasedpu0 sonic-db-cli DPU_APPL_DB FLUSHDB' >/dev/null 2>&1
pre_eni=$(sshpass -p password ssh -o StrictHostKeyChecking=no -p $SSHPORT admin@$HOST \
  'sudo sonic-db-cli COUNTERS_DB HLEN COUNTERS_ENI_NAME_MAP' 2>/dev/null | tr -d '\r' | tail -1)
echo "Pre-push ENI count on DPU0: $pre_eni"

# Background samplers
(
  while :; do
    t=$(date +%s.%N)
    n=$(sshpass -p password ssh -o StrictHostKeyChecking=no -o ConnectTimeout=2 admin@$HOST \
      'sudo docker exec databasedpu0 sonic-db-cli DPU_APPL_DB DBSIZE' 2>/dev/null | tr -d '\r' | tail -1)
    printf '%s %s\n' "$t" "${n:-?}" >> $OUT/dbsize.log
    sleep 0.2
  done
) &
PID_DB=$!
(
  while :; do
    t=$(date +%s.%N)
    n=$(sshpass -p password ssh -o StrictHostKeyChecking=no -o ConnectTimeout=2 -p $SSHPORT admin@$HOST \
      'sudo sonic-db-cli COUNTERS_DB HLEN COUNTERS_ENI_NAME_MAP' 2>/dev/null | tr -d '\r' | tail -1)
    printf '%s %s\n' "$t" "${n:-?}" >> $OUT/eni.log
    sleep 0.2
  done
) &
PID_ENI=$!

T0=$(date +%s.%N)
for f in 000apl 000eni 000map; do
  t0=$(date +%s.%N)
  python3 gnmi_client.py --batch_val 3000 -l info \
    -t ${HOST}:${PORT} -i 0 -n 8 \
    update -f $CFG/dpu0/pl_100.dpu0.${f}.json 2>&1 \
    | tee $OUT/client_${f}.log \
    | grep -E '^elapsed:|module_imports|json_load|rpc_set|build_setrequest|proto_serialize' | tail -8
  t1=$(date +%s.%N)
  awk -v a="$t0" -v b="$t1" -v ff="$f" \
    'BEGIN { printf "  WALL[%s] %.3fs\n", ff, (b - a) }' < /dev/null
done
T1=$(date +%s.%N)
echo "$T0 push_start" > $OUT/marks.log
echo "$T1 push_end"  >> $OUT/marks.log
awk -v a="$T0" -v b="$T1" -v p="$PLAT" \
  'BEGIN { printf "  TOTAL[%s] %.3fs\n", p, (b - a) }' < /dev/null

# Let SAI catch up
sleep 5
kill $PID_DB $PID_ENI 2>/dev/null
wait $PID_DB $PID_ENI 2>/dev/null

post_eni=$(sshpass -p password ssh -o StrictHostKeyChecking=no -p $SSHPORT admin@$HOST \
  'sudo sonic-db-cli COUNTERS_DB HLEN COUNTERS_ENI_NAME_MAP' 2>/dev/null | tr -d '\r' | tail -1)
dbsize=$(sshpass -p password ssh -o StrictHostKeyChecking=no admin@$HOST \
  'sudo docker exec databasedpu0 sonic-db-cli DPU_APPL_DB DBSIZE' 2>/dev/null | tr -d '\r' | tail -1)
echo "Post-push: databasedpu0 DBSIZE=$dbsize, DPU0 ENI_NAME_MAP count=$post_eni"
echo "Samples: dbsize.log=$(wc -l < $OUT/dbsize.log)  eni.log=$(wc -l < $OUT/eni.log)"
echo "Output dir: $OUT"
