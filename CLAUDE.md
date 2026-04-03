# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

sonic-mgmt is the test infrastructure and management automation repository for SONiC (Software for Open Networking in the Cloud). It contains thousands of pytest-based test cases for functional, performance, and regression testing of SONiC switches against both physical testbeds and virtual switch (VS) topologies.

## Key Concepts

- **Testbed topologies**: Tests target specific topologies (t0, t1, t2, dualtor, smartswitch). Tests marked for `t1` won't work on `t0` testbeds.
- **DUT (Device Under Test)**: The SONiC switch being tested. `duthost` refers to the NPU/main switch.
- **PTF (Packet Test Framework)**: Used for data-plane testing via packet injection/verification.
- **SmartSwitch / DPU**: SmartSwitch is a SONiC switch with on-board DPU modules. `duthost` = NPU handle; `dpuhosts` = list of DPU SONiC instance handles. DPUs are reachable via midplane IP. Dark mode = DPUs shut down; Lit mode = DPUs active.
- **Ansible**: Used to deploy and manage testbed infrastructure.

## Commands

### Setup (one-time)

```bash
# Set up the sonic-mgmt container first (required for testbed commands)
./setup-container.sh -n sonic-mgmt -d /data

# Enter the container
make shell

# Or set up a Python virtualenv for unit tests only
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

### Testbed Management (runs inside Docker container via make)

```bash
make add-topo                              # Deploy default topology (vms-kvm-t0)
make add-topo TOPO=vms-kvm-t1             # Deploy a specific topology
make remove-topo
make deploy-mg                             # Deploy minigraph to DUT
```

### Running Tests

```bash
# Via make (inside container)
make test T=bgp/test_bgp_fact.py
make test T=bgp/test_bgp_fact.py EXTRA='-e "--neighbor_type=sonic"'

# Via run_tests.sh (from tests/ directory inside container)
./run_tests.sh -n <testbed_name> -d <dut_name> -f <testbed_file> -i ../ansible/<inventory> -c <test_path>

# Via pytest directly (from tests/ directory)
pytest test_feature.py -v --testbed=vms-kvm-t0 --inventory=../ansible/veos_vtb
pytest -m "topology_t0" test_bgp.py
pytest --collect-only   # validate test collection without running
```

### Linting and Code Quality

```bash
# Install and run all pre-commit hooks
pip install pre-commit
pre-commit install
pre-commit run --all-files

# Run individual linters
flake8 --max-line-length=120 <file>
pylint --rcfile=pylintrc <file>

# tests/common2/ only (stricter rules)
black tests/common2/
isort --profile black tests/common2/
mypy tests/common2/
```

## Code Architecture

### Test Organization

- **`tests/`** — Main pytest test suite. Each subdirectory tests a specific feature or protocol (bgp/, acl/, vlan/, platform_tests/, smartswitch/, etc.).
- **`tests/conftest.py`** (~174KB) — Root fixture hub. Loads ~15 pytest plugins and defines core fixtures: `duthosts`, `ptfhost`, `tbinfo`, `rand_one_dut_hostname`, `enum_frontend_dut_hostname`, `dpuhosts`.
- **`tests/common/`** — Shared utilities and helpers used across test modules.
- **`tests/common2/`** — Newer test infrastructure with stricter linting (black, isort, mypy strict mode, pylint all enforced).
- **`tests/pytest.ini`** — Defines all valid pytest markers. New tests must use markers defined here.

### Ansible Layer

- **`ansible/`** — Playbooks and roles for deploying and managing testbed infrastructure. `testbed.yaml` defines topology configurations.

### Other Frameworks

- **`spytest/`** — Alternative test framework (not the primary one). Has its own pytest.ini and independent execution.
- **`test_reporting/`** — Tools for uploading JUnit XML results to Kusto/Azure Data Explorer.
- **`sdn_tests/`** — SDN-specific tests using Ondatra/Go (excluded from main CI pre-commit hooks).

## Writing Tests

```python
import pytest
from tests.common.helpers.assertions import pytest_assert

@pytest.mark.topology('t0')
def test_my_feature(duthosts, rand_one_dut_hostname, tbinfo):
    """Test that my feature works correctly."""
    duthost = duthosts[rand_one_dut_hostname]

    duthost.shell('config my_feature enable')
    output = duthost.show_and_parse('show my_feature status')
    pytest_assert(output[0]['status'] == 'enabled', "Feature should be enabled")
```

Key rules:
- Always mark tests with `@pytest.mark.topology(...)` using a topology from `tests/pytest.ini`.
- Use `wait_until` helpers instead of `time.sleep` for network state changes.
- Tests must be idempotent — clean up after themselves and restore state.
- Never hardcode IPs or ports — use fixtures and `tbinfo`.
- For multi-ASIC platforms, use the appropriate multi-ASIC fixtures.
- Add `@pytest.mark.flaky(reruns=3)` for inherently flaky network tests.

### Common DUT Patterns

```python
duthost.shell('show interfaces status')           # Run CLI command
duthost.show_and_parse('show vlan brief')         # Run and parse tabular output
duthost.is_service_running('swss')                # Check service status

# PTF data-plane testing
ptf_runner(duthost, ptfhost, 'my_ptf_test',
           platform_dir='ptftests',
           params={'router_mac': router_mac})
```

## Linting Rules by Directory

| Directory | flake8 | black | isort | mypy | pylint |
|-----------|--------|-------|-------|------|--------|
| `tests/` (general) | max-line-length=120 | — | — | — | — |
| `spytest/` | 120, ignore E1/E2/E3/E5/E7/W5 | — | — | — | — |
| `tests/common2/` | 120, ignore E1/E2/E3/E5/W1/W2/W3/W5 | yes (120) | yes (black profile) | strict | yes |
| `tests/gnmi/protos/`, `tests/common/sai_validation/gnmi*.py` | excluded (generated) | — | — | — | — |

## Work History / Session Notes

### DASH API Load Speed Test (`tests/dash/test_dash_api_speed_pl.py`)

**Branch:** `work-api-load-speed-test`

This test measures the time to push private-link-50 DASH configs onto a DPU via gNMI.

**Key files:**
- `tests/dash/test_dash_api_speed_pl.py` — main speed test
- `tests/dash/gnmi_utils.py` — gNMI helpers (`write_gnmi_files`, `gnmi_set`, `apply_messages`, `apply_gnmi_file`)
- `tests/dash/proto_utils.py` — protobuf serialization helpers
- `tests/dash/configs/private-link-50/dpu<N>/` — JSON config files (one per table type, per DPU)

**Architecture:**
- Config JSON files contain a list of `{"OP": "SET"|"DEL", "<TABLE>:<KEY>": {...}}` operations.
- Each entry is serialized to protobuf via `proto_utils.parse_dash_proto`, written to a local temp dir (`env.work_dir`), then pushed via gNMI path `/DPU_APPL_DB/dpu<N>/<TABLE>[key=<KEY>]:$/root/<file>`.
- `write_gnmi_files` does: tar the temp dir → scp to PTF → `gnmi_set` on PTF → cleanup.
- ENI verification: poll `COUNTERS_DB HGETALL COUNTERS_ENI_NAME_MAP` on DPU until 64 ENIs appear.

**Evolution of the test (most recent at top):**
1. *(2026-04-02)* Reverted serialization optimizations. Now pushes **one config file at a time** through the full gNMI CLI flow (serialize → tar → SCP → gnmi_set → cleanup) to isolate whether ENI config reaches the DPU correctly.
2. *(earlier)* Single-pass optimization: all files serialized first (Phase 1, no SSH), then one combined tar+SCP+gnmi_set (Phase 2). Goal was to reduce SSH overhead from O(N) to O(1).
3. *(earlier)* Added 1-second sleep between gNMI batches in `write_gnmi_files` to reduce server overload.
4. *(earlier)* gNMI cert sync: before pushing, CA + client certs are copied from the NPU gnmi container to PTF so they always match.
5. *(earlier)* DPU network setup: adds Loopback0 IP, removes midplane default routes, adds permanent static ARP entries on NPU for dataplane next-hops.

**Run command (from test server):**
```bash
cd /home/dash/sonic-mgmt/sonic-mgmt/tests && ./run_tests.sh \
  -n keysight-ss01 \
  -d keysight-ss01 \
  -f ../ansible/testbed.yaml \
  -i ../ansible/lab \
  -c dash/test_dash_api_speed_pl.py \
  -e "--dpu_index=0"
```

**Open question being investigated:** Does the one-at-a-time gNMI push method correctly get ENI config onto the DPU?

---

## Commits and PRs

- **Commit format**: `[component/folder]: Description` with `Signed-off-by` line (`git commit -s`). CLA signing via EasyCLA is required for upstream contributions.
- **PR description**: Use `.github/PULL_REQUEST_TEMPLATE.md` — fill in all sections including topology markers, platform specifics, and backport requests.
- **New tests**: Must be validated on at least a VS topology before merging.
