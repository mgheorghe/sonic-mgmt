#!/bin/bash
# After NPUs are up, wait for DPU0 midplane reachability on both, then enable NAT
NV=10.36.78.150
CI=10.36.77.121

for i in $(seq 1 30); do
  ts=$(date +%H:%M:%S)
  nv_dpu=$(sshpass -p password ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 admin@$NV \
    'ping -c1 -W2 169.254.200.1 >/dev/null 2>&1 && echo UP || echo DOWN' 2>/dev/null | tail -1)
  ci_dpu=$(sshpass -p password ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 admin@$CI \
    'ping -c1 -W2 169.254.200.1 >/dev/null 2>&1 && echo UP || echo DOWN' 2>/dev/null | tail -1)
  printf '[%s] try=%d nvidia_dpu0=%s cisco_dpu0=%s\n' "$ts" "$i" "${nv_dpu:-DOWN}" "${ci_dpu:-DOWN}"
  if [ "$nv_dpu" = "UP" ] && [ "$ci_dpu" = "UP" ]; then
    echo BOTH_DPU0_UP
    break
  fi
  sleep 20
done

echo "--- Enabling NAT port-forwards on both NPUs ---"
sshpass -p password ssh -o StrictHostKeyChecking=no admin@$NV \
  'sudo sonic-dpu-mgmt-traffic.sh inbound -e --dpus all --ports 5021,5022,5023,5024 2>&1 | tail -3'
echo "--- Cisco NAT ---"
sshpass -p password ssh -o StrictHostKeyChecking=no admin@$CI \
  'sudo sonic-dpu-mgmt-traffic.sh inbound -e --dpus all --ports 5021,5022,5023,5024,5025,5026,5027,5028 2>&1 | tail -3'

echo "--- Trying DPU0 SSH on each ---"
echo -n "nvidia dpu0 via :5021 → "
sshpass -p password ssh -o StrictHostKeyChecking=no -o ConnectTimeout=8 -p 5021 admin@$NV 'hostname' 2>&1 | tail -1
echo -n "cisco dpu0 via :5021  → "
sshpass -p password ssh -o StrictHostKeyChecking=no -o ConnectTimeout=8 -p 5021 admin@$CI 'hostname' 2>&1 | tail -1
