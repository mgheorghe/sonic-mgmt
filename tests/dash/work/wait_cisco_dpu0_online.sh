#!/bin/bash
# Wait for Cisco MtFuji DPU0 to actually go Online after a reboot.
#
# Watches `show chassis modules status` every 20s and reports the row for
# DPU0 plus the midplane ARP entry. Exits when DPU0's Oper-Status is
# "Online" (chassisd has fully programmed it AND the midplane is reachable).
#
# Walks through the expected state machine:
#   1. SSH unreachable        (NPU rebooting)
#   2. SSH OK, no chassisd    (pmon hasn't started yet)
#   3. DPU0 row missing       (CHASSIS_MODULE_TABLE not populated)
#   4. DPU0 Offline, Desc=N/A (chassisd up but DPU not detected)
#   5. DPU0 Offline, Desc=AMD Pensando DSC, Serial=...  (detected, powering on)
#   6. DPU0 Partial Online    (midplane up, control plane not yet)
#   7. DPU0 Online            <-- target
NPU=10.36.77.120

for i in $(seq 1 60); do
  ts=$(date +%H:%M:%S)
  row=$(sshpass -p password ssh -o StrictHostKeyChecking=no -o ConnectTimeout=4 admin@$NPU \
        'show chassis modules status 2>&1 | awk "/^  DPU0 /"' 2>/dev/null | head -1 | tr -s ' ')
  arp=$(sshpass -p password ssh -o StrictHostKeyChecking=no -o ConnectTimeout=4 admin@$NPU \
        'sudo arp -n | awk "/^169\\.254\\.200\\.1 /"' 2>/dev/null | head -1 | tr -s ' ')

  if [ -z "$row" ]; then
    printf '[%s] try=%d  SSH/chassis not ready yet\n' "$ts" "$i"
  else
    printf '[%s] try=%d  %s   ARP: %s\n' "$ts" "$i" "$row" "${arp:-no-arp}"
    oper=$(echo "$row" | awk '{print $3}')
    if [ "$oper" = "Online" ]; then
      echo "DPU0_ONLINE"
      exit 0
    fi
  fi
  sleep 20
done

echo "TIMEOUT after 60 tries (20 minutes)"
exit 1
