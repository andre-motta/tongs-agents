"""Bounded, never-raising environment reads shared by the hook and terminal adapters.

Kept dependency-free so the hook can import it without loading
``tongs_agents.terminal_apis``.
"""

import os

MAX_VALUE_BYTES = 1024


def bounded_env_value(name: str) -> str | None:
    """``name`` from the environment when it is text without NUL within the byte limit."""
    try:
        value = os.environ.get(name)
        if (
            not isinstance(value, str)
            or "\0" in value
            or len(value.encode("utf-8")) > MAX_VALUE_BYTES
        ):
            return None
    except Exception:
        return None
    return value
