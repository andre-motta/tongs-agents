"""The sandbox flavour of the emitters (docs/work/openshell-sandbox.md, rows 8, 12, 13).

A sandboxed harness gets a fresh config directory that tongs-agents uploads
into the sandbox, so nothing here merges with user files or records a
manifest: ``sandbox_files`` returns the whole tree as relative POSIX paths and
bytes. It differs from the host emitters in three ways:

- hooks spool the raw hook payload with POSIX ``sh`` and coreutils (no Python
  in the image); the host bridge parses it on import;
- no MCP server is emitted: neither the tongs-agents fleet and control tools
  (row 12) nor the user's servers, whose commands are host paths;
- no credential, environment value or host path is written (row 13).

Skills and instructions from the canonical config are copied when one is
given, scoped by harness and profile name as on the host.
"""

from __future__ import annotations

import json
import shlex
from importlib.resources import files
from pathlib import Path, PurePosixPath
from typing import Any

from tongs_agents import cli, owned
from tongs_agents.config_layer.emitters import _common
from tongs_agents.config_layer.emitters.claude import IMPORT_LINE, PLUGIN_DESCRIPTION

SPOOL_ENV = "TONGS_AGENTS_SPOOL"
SPOOL_EVENT_ARG0 = "tongs-agents-spool"
PI_EXTENSION_NAME = "tongs-agents.js"
CONFIG_DIR_ENV_BY_HARNESS = {
    "claude": "CLAUDE_CONFIG_DIR",
    "codex": "CODEX_HOME",
    "pi": "PI_CODING_AGENT_DIR",
}

# One event, one file: written to a dot-prefixed temporary name in the spool
# and renamed to <19-digit nanoseconds>-<pid>-<event>.json. Always exits 0 so
# a full disk or a missing spool never blocks the agent.
SPOOL_SCRIPT = (
    'd="${' + SPOOL_ENV + ':-}"; [ -n "$d" ] || exit 0; umask 077; '
    'mkdir -p -- "$d" 2>/dev/null || exit 0; '
    't=$(date +%s%N 2>/dev/null); '
    'case "$t" in ""|*[!0-9]*) t=$(date +%s)000000000;; esac; '
    't=$(printf "%019d" "$t" 2>/dev/null) || exit 0; '
    'f="$d/.$t-$$.tmp"; '
    'if cat > "$f"; then mv -f -- "$f" "$d/$t-$$-$1.json"; else rm -f -- "$f"; fi; '
    'exit 0'
)


def spool_hook_command(event: str) -> str:
    """The hook command line for ``event`` (run by the harness through a shell)."""
    return shlex.join(["sh", "-c", SPOOL_SCRIPT, SPOOL_EVENT_ARG0, event])


def _hooks_document(harness: str) -> dict[str, Any]:
    events = cli.CLAUDE_HOOK_EVENTS if harness == "claude" else cli.CODEX_HOOK_EVENTS
    hooks: dict[str, list] = {}
    for event, arg in events:
        group: dict[str, Any] = {"hooks": [{
            "type": "command",
            "command": spool_hook_command(arg),
            "timeout": 3 if event == "SessionEnd" else 5,
        }]}
        if harness == "codex" and event == "SessionStart":
            group["matcher"] = "startup|resume|clear"
        hooks[event] = [group]
    return {"hooks": hooks}


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def pi_extension_source() -> bytes:
    source = files("tongs_agents").joinpath("pi_extension", "extensions", "fleet-monitor.js")
    return source.read_bytes()


def _selected(config: Any, harness: str, profile: str | None):
    """``(skills as (name, directory), instructions or None)`` for this harness and profile.

    Takes the loaded canonical config (``schema.CanonicalConfig``) or anything
    ``_common.coerce_config`` accepts.
    """
    names = () if profile is None else (profile,)
    if hasattr(config, "skills_for") and hasattr(config, "instructions_for"):
        skills = [(skill.name, skill.path) for skill in config.skills_for(harness, names)]
        return skills, config.instructions_for(harness) or None
    canonical = _common.coerce_config(config)
    skills = [
        (skill.name, skill.source) for skill in canonical.skills
        if _common.applies(skill.scope, harness, names)
    ]
    return skills, _common.render_instructions(canonical, harness)


def _skill_files(base: PurePosixPath, skills) -> dict[str, bytes]:
    result = {}
    for name, source in skills:
        for relative, fmt, text in _common.skill_files(source, name):
            data: Any = text
            if fmt == "json":
                try:
                    data = json.loads(text)
                except ValueError:
                    raise _common.EmitError(f"skills: {name}/{relative}: not valid JSON") from None
            path = base / name / relative
            result[str(path)] = owned.render_owned(
                fmt, data, owner=_common.OWNER, schema_version=_common.SCHEMA_VERSION,
            )
    return result


def sandbox_files(harness: str, *, config: Any = None,
                  profile: str | None = None) -> dict[str, bytes]:
    """The harness config tree for a sandbox, relative to its config directory.

    ``harness`` other yields an empty tree: its status comes from the
    sandbox phase only. Raises EmitError for a canonical config that cannot
    be emitted.
    """
    if harness not in CONFIG_DIR_ENV_BY_HARNESS:
        return {}
    tree: dict[str, bytes] = {}
    skills: list = []
    body = None
    if config is not None:
        skills, body = _selected(config, harness, profile)
    if harness == "claude":
        tree["settings.json"] = _json_bytes(_hooks_document("claude"))
        if skills:
            bundle = PurePosixPath("skills") / _common.BUNDLE
            tree[str(bundle / ".claude-plugin" / "plugin.json")] = _json_bytes(
                {"name": _common.BUNDLE, "description": PLUGIN_DESCRIPTION}
            )
            tree.update(_skill_files(bundle / "skills", skills))
        if body is not None:
            tree["tongs-agents/instructions.md"] = body.encode("utf-8")
            tree["CLAUDE.md"] = (IMPORT_LINE + "\n").encode("utf-8")
    elif harness == "codex":
        tree["hooks.json"] = _json_bytes(_hooks_document("codex"))
        tree.update(_skill_files(PurePosixPath("skills") / _common.BUNDLE, skills))
        if body is not None:
            tree["AGENTS.md"] = body.encode("utf-8")
    else:
        tree[f"extensions/{PI_EXTENSION_NAME}"] = pi_extension_source()
        tree.update(_skill_files(PurePosixPath("skills") / _common.BUNDLE, skills))
        if body is not None:
            tree["AGENTS.md"] = body.encode("utf-8")
    return tree


def write_tree(tree: dict[str, bytes], into: Path) -> None:
    """Write ``tree`` under the empty private directory ``into``."""
    for relative, data in sorted(tree.items()):
        path = PurePosixPath(relative)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise _common.EmitError(f"refusing to write {relative!r}")
        target = into.joinpath(*path.parts)
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with open(target, "xb") as stream:
            stream.write(data)
