"""``python -m tongs_agents.sandbox sync``: one bridge pass for headless use."""

from __future__ import annotations

import argparse
import sys

from tongs_agents.sandbox import bridge


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tongs_agents.sandbox",
        description="Import hook events from sandboxed agents into the session store.",
    )
    parser.add_argument("command", choices=["sync"])
    parser.parse_args(argv)
    result = bridge.sync_all()
    print(
        f"{result.launches} sandboxed launch(es): {result.imported} event(s) imported, "
        f"{result.rejected} rejected, {result.phase_records} phase update(s)"
    )
    for error in result.errors:
        print(f"  {error}", file=sys.stderr)
    return 1 if result.errors else 0


if __name__ == "__main__":
    sys.exit(main())
