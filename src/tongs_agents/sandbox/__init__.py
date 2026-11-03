"""OpenShell sandboxes for agent profiles (docs/sandbox.md).

- ``openshell``: the argv-only CLI wrapper (version floor, bounded output).
- ``launcher``: creates or reuses a profile's sandbox and builds the pane command.
- ``state``: per-launch records the bridge reads.
- ``bridge``: pulls hook spools from sandboxes and imports session records.

Used by ``launch`` and ``sessions_feed``; the hook never imports it.
"""
