#!/usr/bin/env python3
"""Fix gnmi_client.py imports so `--no-proto` actually disables proto serialization.

Background:
- gnmi_agent/__init__.py does `sys.path.insert(0, <pkg dir>)`, so the package's
  proto_utils.py can be loaded under TWO different names:
    * `gnmi_agent.proto_utils`     (via `from gnmi_agent import proto_utils`)
    * `proto_utils`                (top-level, via `import proto_utils`)
- gnmi_client.py used `from gnmi_agent import proto_utils` and then set
  `proto_utils.ENABLE_PROTO = False` when --no-proto was passed.
- BUT go_gnmi_utils.py uses `import proto_utils` (top-level), so it consults a
  DIFFERENT module instance whose ENABLE_PROTO is still True.
- Result: --no-proto flag was silently ineffective; map files still got full
  proto serialization (~23 s for 64 K vnet mappings on the test machine).

Fix: make gnmi_client.py use the same top-level `proto_utils` module that
go_gnmi_utils.py uses. The `import gnmi_agent` line first triggers the
sys.path-insert in the package's __init__, then `import proto_utils` finds
the same module instance go_gnmi_utils sees.
"""
import sys

PATH = '/home/dash/sonic-mgmt/sonic-mgmt/gnmi/gnmi_client.py'

with open(PATH) as f:
    src = f.read()

# Find any messed-up state from a prior aborted sed and clean it
broken = (
    "# Same proto_utils as go_gnmi_utils uses (top-level)\n"
    "nimport gnmi_agent  # noqa: F401  triggers sys.path injection for top-level proto_utils\n"
    "import proto_utils\n"
)
fixed = (
    "import gnmi_agent  # noqa: F401  side-effect: __init__ inserts pkg dir into sys.path\n"
    "import proto_utils  # same module instance go_gnmi_utils.py uses\n"
)
if broken in src:
    src = src.replace(broken, fixed)
elif "from gnmi_agent import proto_utils" in src:
    src = src.replace(
        "from gnmi_agent import proto_utils",
        "import gnmi_agent  # noqa: F401  side-effect: __init__ inserts pkg dir into sys.path\n"
        "import proto_utils  # same module instance go_gnmi_utils.py uses",
    )
else:
    print("Already patched or unknown state", file=sys.stderr)
    sys.exit(0)

with open(PATH, 'w') as f:
    f.write(src)
print("PATCHED")
