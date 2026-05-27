#!/bin/bash
# poll until both NPUs are SSH-reachable again post-reboot
for i in $(seq 1 30); do
  ts=$(date +%H:%M:%S)
  nv=$(sshpass -p password ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 admin@10.36.78.150 'echo OK' 2>/dev/null | tail -1)
  ci=$(sshpass -p password ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 admin@10.36.77.121 'echo OK' 2>/dev/null | tail -1)
  printf '[%s] try=%d nvidia=%s cisco=%s\n' "$ts" "$i" "${nv:-DOWN}" "${ci:-DOWN}"
  if [ "$nv" = "OK" ] && [ "$ci" = "OK" ]; then
    echo ALL_NPUS_UP
    exit 0
  fi
  sleep 20
done
echo TIMEOUT
exit 1
