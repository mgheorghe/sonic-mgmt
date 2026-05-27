#!/bin/bash
# Pull /var/log tarballs from the 10.36.77.120 (MtFuji) Cisco NPU + DPU0
# and Nvidia 10.36.78.150 NPU + DPU0 into a timestamped dir on SMD.

set -o pipefail

NV_NPU=10.36.78.150
CI_NPU=10.36.77.120
TS=$(date +%Y%m%d_%H%M%S)
OUT=/tmp/dash_logs_run3_$TS
mkdir -p $OUT

grab() {
  local label="$1"; local ip="$2"; local port="$3"
  echo "=== $label ($ip:$port) ==="
  if [ "$port" = "22" ]; then
    sshpass -p password ssh -o StrictHostKeyChecking=no admin@$ip \
      "sudo tar czf /tmp/varlog.tgz -C / var/log 2>&1 | tail -3"
    sshpass -p password scp -o StrictHostKeyChecking=no admin@$ip:/tmp/varlog.tgz $OUT/${label}_varlog.tgz
    sshpass -p password ssh -o StrictHostKeyChecking=no admin@$ip "sudo rm /tmp/varlog.tgz"
  else
    sshpass -p password ssh -o StrictHostKeyChecking=no -p $port admin@$ip \
      "sudo tar czf /tmp/varlog.tgz -C / var/log 2>&1 | tail -3"
    sshpass -p password scp -o StrictHostKeyChecking=no -P $port admin@$ip:/tmp/varlog.tgz $OUT/${label}_varlog.tgz
    sshpass -p password ssh -o StrictHostKeyChecking=no -p $port admin@$ip "sudo rm /tmp/varlog.tgz"
  fi
  ls -lh $OUT/${label}_varlog.tgz
}

grab nvidia_npu  $NV_NPU 22
grab nvidia_dpu0 $NV_NPU 5021
grab cisco_npu   $CI_NPU 22
grab cisco_dpu0  $CI_NPU 5021

echo
echo "=== Summary ==="
ls -lh $OUT/
echo "LOGS_DIR=$OUT"
