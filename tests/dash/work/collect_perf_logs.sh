#!/bin/bash
# Collect targeted log slices to break down 1-ENI gnmi push performance.
#
# Pulls, from each NPU and DPU:
#   - syslog (host /var/log/syslog)
#   - gnmi container log (gnmi-native server timing)
#   - swss container syslog (orchagent)
#   - syncd container syslog (sairedis ASIC ops)
#
# Push windows (UTC):
#   Cisco MtFuji  (10.36.77.120) DPU0: 23:04:07 -> 23:04:16
#   Nvidia       (10.36.78.150) DPU0: 22:40:52 -> 22:41:11

set -o pipefail
NV_NPU=10.36.78.150
CI_NPU=10.36.77.120
TS=$(date +%Y%m%d_%H%M%S)
OUT=/tmp/dash_perf_$TS
mkdir -p $OUT

dump_host() {
  local label="$1"; local ip="$2"; local port="$3"
  echo "=== $label  syslog tail ==="
  sshpass -p password ssh -o StrictHostKeyChecking=no ${port:+-p $port} admin@$ip \
    "sudo tail -2000 /var/log/syslog 2>/dev/null" > $OUT/${label}_syslog.txt
  wc -l $OUT/${label}_syslog.txt
  echo "=== $label  gnmi container log ==="
  sshpass -p password ssh -o StrictHostKeyChecking=no ${port:+-p $port} admin@$ip \
    "sudo docker logs gnmi 2>&1 | tail -2000" > $OUT/${label}_gnmi.txt 2>&1 || true
  wc -l $OUT/${label}_gnmi.txt
  echo "=== $label  swss container log ==="
  sshpass -p password ssh -o StrictHostKeyChecking=no ${port:+-p $port} admin@$ip \
    "sudo docker logs swss 2>&1 | tail -2000" > $OUT/${label}_swss.txt 2>&1 || true
  wc -l $OUT/${label}_swss.txt
  echo "=== $label  syncd container log ==="
  sshpass -p password ssh -o StrictHostKeyChecking=no ${port:+-p $port} admin@$ip \
    "sudo docker logs syncd 2>&1 | tail -2000" > $OUT/${label}_syncd.txt 2>&1 || true
  wc -l $OUT/${label}_syncd.txt
  echo "=== $label  date ==="
  sshpass -p password ssh -o StrictHostKeyChecking=no ${port:+-p $port} admin@$ip 'date' > $OUT/${label}_date.txt
  cat $OUT/${label}_date.txt
}

dump_host nvidia_npu  $NV_NPU
dump_host nvidia_dpu0 $NV_NPU 5021
dump_host cisco_npu   $CI_NPU
dump_host cisco_dpu0  $CI_NPU 5021

# Also grab the per-DPU databasedpu0 container log on each NPU
for who in "nvidia_npu $NV_NPU" "cisco_npu $CI_NPU"; do
  set -- $who
  echo "=== $1  databasedpu0 ==="
  sshpass -p password ssh -o StrictHostKeyChecking=no admin@$2 \
    "sudo docker logs databasedpu0 2>&1 | tail -1000" > $OUT/${1}_databasedpu0.txt 2>&1 || true
  wc -l $OUT/${1}_databasedpu0.txt
done

echo
echo "=== Summary ==="
ls -lh $OUT/
echo "LOGS_DIR=$OUT"
