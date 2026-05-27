#!/bin/bash
# Tar up /var/log on Nvidia NPU, Nvidia DPU1, Cisco NPU and pull to SMD.
set -o pipefail
NV_NPU=10.36.78.150
CI_NPU=10.36.77.121
TS=$(date +%Y%m%d_%H%M%S)
OUT=/tmp/dash_logs_$TS
mkdir -p $OUT

echo "=== Nvidia NPU /var/log ==="
sshpass -p password ssh -o StrictHostKeyChecking=no admin@$NV_NPU \
  "sudo tar czf /tmp/varlog.tgz -C / var/log 2>&1 | tail -3"
sshpass -p password scp -o StrictHostKeyChecking=no \
  admin@$NV_NPU:/tmp/varlog.tgz $OUT/nvidia_npu_varlog.tgz
sshpass -p password ssh -o StrictHostKeyChecking=no admin@$NV_NPU "sudo rm /tmp/varlog.tgz"
ls -la $OUT/nvidia_npu_varlog.tgz

echo "=== Nvidia DPU1 /var/log (via NAT :5022) ==="
sshpass -p password ssh -o StrictHostKeyChecking=no -p 5022 admin@$NV_NPU \
  "sudo tar czf /tmp/varlog.tgz -C / var/log 2>&1 | tail -3"
sshpass -p password scp -o StrictHostKeyChecking=no -P 5022 \
  admin@$NV_NPU:/tmp/varlog.tgz $OUT/nvidia_dpu1_varlog.tgz
sshpass -p password ssh -o StrictHostKeyChecking=no -p 5022 admin@$NV_NPU "sudo rm /tmp/varlog.tgz"
ls -la $OUT/nvidia_dpu1_varlog.tgz

echo "=== Cisco NPU /var/log ==="
sshpass -p password ssh -o StrictHostKeyChecking=no admin@$CI_NPU \
  "sudo tar czf /tmp/varlog.tgz -C / var/log 2>&1 | tail -3"
sshpass -p password scp -o StrictHostKeyChecking=no \
  admin@$CI_NPU:/tmp/varlog.tgz $OUT/cisco_npu_varlog.tgz
sshpass -p password ssh -o StrictHostKeyChecking=no admin@$CI_NPU "sudo rm /tmp/varlog.tgz"
ls -la $OUT/cisco_npu_varlog.tgz

echo "=== Cisco DPU0 (unreachable post-reboot — skipping) ==="

echo "=== Summary ==="
ls -lh $OUT/
echo "LOGS_DIR=$OUT"
