#!/bin/bash
# Run DASH speed test against Nvidia keysight-nss01-dpu1, capture timing.
set -o pipefail
cd /home/dash/sonic-mgmt/sonic-mgmt/tests
export ANSIBLE_LIBRARY=/home/dash/sonic-mgmt/sonic-mgmt/ansible/library
export ANSIBLE_MODULE_UTILS=/home/dash/sonic-mgmt/sonic-mgmt/ansible/module_utils

LOG=/tmp/dash_nvidia_eni000_$(date +%Y%m%d_%H%M%S).log

pytest dash/test_dash_api_speed_pl.py \
    --testbed=keysight-nss01 \
    --testbed_file=../ansible/testbed.yaml \
    --inventory=../ansible/lab \
    --host-pattern=keysight-nss01 \
    --dpu_index=0 \
    --dpu-pattern=keysight-nss01-dpu1 \
    --cache-clear -v \
    --log-file=$LOG --log-file-level=DEBUG 2>&1 | tee /tmp/dash_nvidia_stdout.log

echo
echo "===== TIMINGS GREP ====="
grep -E "elapsed|elapsed_total|push (took|done)|^  \[1/1\]|gnmi push|gnmi_set|TOTAL|pushed.*ENI" /tmp/dash_nvidia_stdout.log | tail -40
echo
echo "===== LOG FILE: $LOG ====="
