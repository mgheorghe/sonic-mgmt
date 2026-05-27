#!/bin/bash
# Run DASH speed test against Cisco keysight-css01-dpu0.
set -o pipefail
cd /home/dash/sonic-mgmt/sonic-mgmt/tests
export ANSIBLE_LIBRARY=/home/dash/sonic-mgmt/sonic-mgmt/ansible/library
export ANSIBLE_MODULE_UTILS=/home/dash/sonic-mgmt/sonic-mgmt/ansible/module_utils

LOG=/tmp/dash_cisco_eni000_$(date +%Y%m%d_%H%M%S).log

rm -rf _cache

pytest dash/test_dash_api_speed_pl.py \
    --testbed=keysight-css01 \
    --testbed_file=../ansible/testbed.yaml \
    --inventory=../ansible/lab \
    --host-pattern=keysight-css01 \
    --dpu_index=0 \
    --dpu-pattern=keysight-css01-dpu0 \
    --cache-clear -v \
    --log-file=$LOG --log-file-level=DEBUG 2>&1 | tee /tmp/dash_cisco_stdout.log

echo
echo "===== KEY MARKERS ====="
grep -E "pushing |done    |TOTAL|elapsed:|ENIs found|Files loaded|FAILED|PASSED|short test summary" /tmp/dash_cisco_stdout.log | tail -40
echo
echo "===== LOG FILE: $LOG ====="
