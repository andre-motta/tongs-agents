"""Claude Code emitter (docs/work/emitter-strategy.md, rows 1 to 4).

- Skills: an owned skills-dir plugin at ``<claude>/skills/tongs-agents/``
  (``.claude-plugin/plugin.json`` plus ``skills/<skill>/...``), copied.
- Instructions: owned ``<claude>/tongs-agents/instructions.md`` and one exact
  ``@tongs-agents/instructions.md`` line in ``<claude>/CLAUDE.md``.
- MCP: ``claude mcp add --scope user`` and ``remove`` with
  ``CLAUDE_CONFIG_DIR`` set per target; no ``env`` is written, because stdio
  servers inherit Claude's environment.
- Hooks: our keyed entries in ``<claude>/settings.json``, as ``install`` writes.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any

from tongs_agents import cli, owned
from tongs_agents.config_layer.emitters import _common
from tongs_agents.config_layer.emitters.base import BaseEmitter
from tongs_agents.config_layer.protocol import CanonicalFragment, ProfileTarget

IMPORT_LINE = "@tongs-agents/instructions.md"
PLUGIN_DESCRIPTION = "Skills emitted by tongs-agents from the dotfiles repo"


def plugin_dir(directory: Path) -> Path:
    return directory / "skills" / _common.BUNDLE


def user_config(target: ProfileTarget) -> Path:
    """Where Claude keeps user-scope MCP servers for this target."""
    if target.config_dir is None:
        return Path.home() / ".claude.json"
    return _common.config_dir(target) / ".claude.json"


def normalize(entry: Any) -> Any:
    """The fields tongs-agents sets on a Claude MCP entry, as Claude stores them."""
    if not isinstance(entry, dict):
        return {"unrecognized": entry}
    result: dict[str, Any] = {}
    if "url" in entry:
        result["url"] = entry["url"]
    else:
        result["command"] = entry.get("command")
        result["args"] = entry.get("args", [])
    for key in ("env", "headers"):
        if entry.get(key):
            result[key] = entry[key]
    if "cwd" in entry:
        result["cwd"] = entry["cwd"]
    return result


def _skill_data(fmt: str, text: str, where: str) -> Any:
    if fmt != "json":
        return text
    try:
        value = json.loads(text)
    except ValueError:
        raise _common.EmitError(f"{where}: not valid JSON") from None
    if not isinstance(value, dict) or any(key in owned.MARKER_KEYS for key in value):
        raise _common.EmitError(
            f"{where}: a JSON skill file must be an object without "
            + ", ".join(owned.MARKER_KEYS)
        )
    return value


def skill_tree_files(base: Path, skills: list[_common.Skill]) -> dict[Path, tuple[str, Any]]:
    """Owned files for ``skills`` copied under ``base/<skill>/``."""
    files = {}
    for skill in skills:
        for relative, fmt, text in _common.skill_files(skill.source, skill.name):
            where = f"skills: {skill.name}/{relative}"
            files[base / skill.name / relative] = (fmt, _skill_data(fmt, text, where))
    return files


class ClaudeEmitter(BaseEmitter):
    harness_id = "claude"

    def plan_full(
        self, config: Any, target: ProfileTarget, *, force: bool = False
    ) -> _common.EmitPlan:
        canonical, plan, previous = self._start(config, target)
        directory = _common.config_dir(target)
        claude = shutil.which("claude")
        if claude is None and _common.skip_absent_home(plan, "claude", directory):
            return plan

        files: dict[Path, tuple[str, Any]] = {}
        skills = _common.selected_skills(canonical, target)
        if skills:
            bundle = plugin_dir(directory)
            files[bundle / ".claude-plugin" / "plugin.json"] = ("json", {
                "name": _common.BUNDLE,
                "description": PLUGIN_DESCRIPTION,
            })
            files.update(skill_tree_files(bundle / "skills", skills))
        body = _common.render_instructions(canonical, "claude")
        if body is not None:
            files[directory / "tongs-agents" / "instructions.md"] = ("md", body)
        _common.plan_owned_tree(plan, files, previous=previous, force=force)

        claude_md = directory / "CLAUDE.md"
        if body is not None or f"md-import:{claude_md}" in previous:
            _common.plan_import_line(plan, claude_md, IMPORT_LINE, body is not None)

        self._plan_hooks(plan, directory, target)
        self._plan_mcp(plan, canonical, target, directory, previous, force, claude)
        return plan

    def _plan_hooks(self, plan: _common.EmitPlan, directory: Path, target: ProfileTarget):
        hook_target = cli.Target("claude", target.config_dir)
        try:
            change = owned.plan_json_edit(
                directory / "settings.json",
                cli._install_edit(hook_target, _common.hook_command()),
                ownership=cli.HOOK_OWNERSHIP,
            )
        except owned.OwnedFileError as error:
            plan.conflict(directory / "settings.json", str(error))
            return
        plan.changes.append(change)

    def _desired_servers(
        self, plan: _common.EmitPlan, canonical: _common.EmitConfig, target: ProfileTarget
    ) -> dict[str, dict[str, Any]]:
        desired = {
            _common.RESERVED_SERVER: {
                "command": sys.executable, "args": ["-m", cli.MCP_MODULE],
            },
        }
        for server in _common.selected_servers(canonical, target):
            if server.url is not None:
                plan.notes.append(
                    f"MCP server {server.name}: HTTP servers are not emitted for Claude Code "
                    "yet (no live evidence for header handling); skipped"
                )
                continue
            if server.cwd is not None:
                plan.notes.append(
                    f"MCP server {server.name}: Claude Code user-scope servers have no "
                    "working directory; skipped"
                )
                continue
            desired[server.name] = {
                "command": _common.expand_home(server.command[0]),
                "args": list(server.command[1:]),
            }
        return desired

    def _plan_mcp(self, plan, canonical, target, directory, previous, force, claude):
        desired = self._desired_servers(plan, canonical, target)
        names = sorted(set(desired) | self._managed_names(previous, "mcp-cli"))
        path = user_config(target)
        if claude is None:
            _common.skip_cli_entries(plan, "claude", path, names)
            return
        try:
            current_raw = _common.read_mcp_servers(path)
        except owned.OwnedFileError as error:
            plan.conflict(path, f"{error}; MCP servers not changed")
            plan.entries.update(
                (key, dict(value)) for key, value in previous.items()
                if value.get("kind") == "mcp-cli"
            )
            return
        env = {"CLAUDE_CONFIG_DIR": None if target.config_dir is None else str(directory)}
        for name in names:
            entry_id = f"mcp:{name}"
            recorded = previous.get(entry_id)
            raw = current_raw.get(name)
            current = None if name not in current_raw else normalize(raw)
            want = desired.get(name)
            action = _common.decide_server(
                plan, path, want, current, recorded,
                adoptable=name == _common.RESERVED_SERVER and cli._is_owned_mcp(raw),
                force=force,
            )
            label = f"Claude Code MCP server {name}"
            _common.report_action(plan, label, action, path=path,
                                  key=f"mcpServers.{name}",
                                  recorded=_common.is_recorded(recorded))
            entry = None if want is None else {
                "kind": "mcp-cli", "key": name, "sha256": _common.digest_value(want),
            }
            if action in ("keep", "adopt"):
                plan.entries[entry_id] = entry
                continue
            if action == "drifted" and recorded is not None:
                plan.entries[entry_id] = dict(recorded)
                continue
            if action not in ("add", "replace", "adopt-replace", "remove"):
                continue
            base = (claude, "mcp")
            if action in ("replace", "adopt-replace", "remove"):
                plan.commands.append(_common.CommandStep(
                    description=f"remove {label}",
                    argv=(*base, "remove", "--scope", "user", name),
                    env=env,
                    entry_id=entry_id if action == "remove" else None,
                    entry=None,
                ))
            if want is not None:
                plan.commands.append(_common.CommandStep(
                    description=f"add {label}",
                    argv=(*base, "add", "--scope", "user", name, "--",
                          want["command"], *want["args"]),
                    env=env,
                    entry_id=entry_id,
                    entry=entry,
                ))
                plan.entries[entry_id] = entry

    def capture(
        self, target: ProfileTarget, *, include_unmanaged: bool = False
    ) -> CanonicalFragment:
        directory = _common.config_dir(target)
        previous = owned.manifest_entries_for(target)
        warnings: list[str] = []
        skills = _common.capture_skill_tree(plugin_dir(directory) / "skills")
        instructions = _common.read_owned_text(
            directory / "tongs-agents" / "instructions.md", "md"
        )
        managed = self._managed_names(previous, "mcp-cli")
        servers = {}
        try:
            current_servers = _common.read_mcp_servers(user_config(target))
        except owned.OwnedFileError as error:
            warnings.append(f"{error}; MCP servers not captured")
            current_servers = {}
        for name, entry in sorted(current_servers.items()):
            if name == _common.RESERVED_SERVER or not isinstance(entry, dict):
                continue
            if name not in managed and not include_unmanaged:
                continue
            label = f"Claude Code MCP server {name}"
            if "url" in entry:
                servers[name] = {"url": entry["url"]}
                continue
            command = entry.get("command")
            args = entry.get("args", [])
            if not isinstance(command, str) or not isinstance(args, list):
                warnings.append(f"{label}: unrecognized entry; skipped")
                continue
            value: dict[str, Any] = {"command": [command, *map(str, args)]}
            names = _common.env_names_from(entry.get("env"), label, warnings)
            if names:
                value["env"] = names
            servers[name] = value
        return _common.fragment(target, {
            "skills": skills,
            "instructions": instructions,
            "mcp_servers": servers,
            "warnings": warnings,
        })
