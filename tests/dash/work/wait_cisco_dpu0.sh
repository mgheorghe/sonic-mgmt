#!/bin/bash
# Poll Cisco DPU0 until it becomes pingable from the Cisco NPU.
CI=10.36.77.121
for i in $(seq 1 40); do
  ts=$(date +%H:%M:%S)
  st=$(sshpass -p password ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 admin@$CI \
        'ping -c1 -W2 169.254.200.1 >/dev/null 2>&1 && echo UP || echo DOWN' 2>/dev/null | tail -1)
  printf '[%s] try=%d cisco_dpu0=%s\n' "$ts" "$i" "$st"
  if [ "$st" = "UP" ]; then
    echo CISCO_DPU0_UP
    exit 0
  fi
  sleep 20
done
echo TIMEOUT
exit 1
