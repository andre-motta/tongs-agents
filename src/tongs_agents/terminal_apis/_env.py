"""Env-only, never-raising capture helpers for terminal adapters."""

from tongs_agents._envcheck import MAX_VALUE_BYTES, bounded_env_value  # noqa: F401


def bounded_env(names: tuple[str, ...]) -> dict[str, str]:
    captured = {}
    for name in names:
        value = bounded_env_value(name)
        if value is not None:
            captured[name] = value
    return captured


def is_decimal_id(value, max_len: int = 12) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= max_len
        and value.isascii()
        and value.isdigit()
    )
