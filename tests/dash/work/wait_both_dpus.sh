#!/bin/bash
# Wait until BOTH the Nvidia DPU1 and Cisco DPU0 are pingable from their NPUs.
NV=10.36.78.150
CI=10.36.77.121

for i in $(seq 1 40); do
  ts=$(date +%H:%M:%S)
  nv=$(sshpass -p password ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 admin@$NV \
        'ping -c1 -W2 169.254.200.2 >/dev/null 2>&1 && echo UP || echo DOWN' 2>/dev/null | tail -1)
  ci=$(sshpass -p password ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 admin@$CI \
        'ping -c1 -W2 169.254.200.1 >/dev/null 2>&1 && echo UP || echo DOWN' 2>/dev/null | tail -1)
  printf '[%s] try=%d nvidia_dpu1=%s cisco_dpu0=%s\n' "$ts" "$i" "${nv:-DOWN}" "${ci:-DOWN}"
  if [ "$nv" = "UP" ] && [ "$ci" = "UP" ]; then
    echo BOTH_DPUS_UP
    exit 0
  fi
  sleep 20
done
echo TIMEOUT
exit 1
