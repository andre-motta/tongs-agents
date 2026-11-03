#!/usr/bin/env python3
"""Stand-in for the claude, codex and pi CLIs on the workspace validator's PATH.

The validator links it into its private bin directory under each harness
name, so no real agent CLI is reachable from the isolated workspace. Each
call appends its name, arguments and config-dir variables to
``$TONGS_AGENTS_FAKE_CLI_LOG`` and exits 0, except ``mcp get``, which reports
that no server is registered. It starts no agent and uses no network.
"""

import json
import os
import sys

CONFIG_ENV = ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "PI_CODING_AGENT_DIR")


def main(argv):
    name = os.path.basename(argv[0])
    arguments = argv[1:]
    log = os.environ.get("TONGS_AGENTS_FAKE_CLI_LOG")
    if log:
        entry = {
            "cli": name,
            "args": arguments,
            "env": {key: os.environ[key] for key in CONFIG_ENV if key in os.environ},
        }
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")
    if arguments[:2] == ["mcp", "get"]:
        print(f"No MCP server named {arguments[2] if len(arguments) > 2 else ''}",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
