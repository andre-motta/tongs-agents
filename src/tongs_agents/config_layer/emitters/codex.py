"""Codex emitter (docs/work/emitter-strategy.md, rows 5 to 8).

- Skills: owned dir ``$CODEX_HOME/skills/tongs-agents/<skill>/``, copied.
- Instructions: a managed block in ``$CODEX_HOME/AGENTS.md``.
- MCP: our ``[mcp_servers.<name>]`` tables in ``$CODEX_HOME/config.toml``
  through the owned-table writer (option C), with ``env_vars`` naming the
  variables a server needs; Codex passes nothing else through. The
  tongs-agents server also gets ``XDG_STATE_HOME`` and
  ``TONGS_AGENTS_STATE_DIR`` so it reads the sessions the hooks write.
- Hooks: our keyed entries in ``$CODEX_HOME/hooks.json``, as ``install`` writes.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from tongs_agents import cli, owned, paths, toml_tables
from tongs_agents.config_layer.emitters import _common
from tongs_agents.config_layer.emitters.base import BaseEmitter
from tongs_agents.config_layer.emitters.claude import skill_tree_files
from tongs_agents.config_layer.protocol import CanonicalFragment, ProfileTarget

STATE_ENV = ("XDG_STATE_HOME", paths.STATE_DIR_ENV)


def skills_dir(directory: Path) -> Path:
    return directory / "skills" / _common.BUNDLE


def _without_enabled(value: Any) -> tuple[Any, bool]:
    if isinstance(value, dict) and value.get("enabled") is False:
        return {key: item for key, item in value.items() if key != "enabled"}, True
    return value, False


class CodexEmitter(BaseEmitter):
    harness_id = "codex"

    def plan_full(
        self, config: Any, target: ProfileTarget, *, force: bool = False
    ) -> _common.EmitPlan:
        canonical, plan, previous = self._start(config, target)
        directory = _common.config_dir(target)

        skills = _common.selected_skills(canonical, target)
        files = skill_tree_files(skills_dir(directory), skills)
        _common.plan_owned_tree(plan, files, previous=previous, force=force)

        body = _common.render_instructions(canonical, "codex")
        agents_md = directory / "AGENTS.md"
        if body is not None or f"md-block:{agents_md}" in previous:
            _common.plan_markdown_block(plan, agents_md, body, previous=previous, force=force)
        if body is not None and (directory / "AGENTS.override.md").exists():
            plan.notes.append(
                f"{directory / 'AGENTS.override.md'} exists and hides AGENTS.md from Codex"
            )

        hook_target = cli.Target("codex", target.config_dir)
        try:
            change = owned.plan_json_edit(
                directory / "hooks.json",
                cli._install_edit(hook_target, _common.hook_command()),
                ownership=cli.HOOK_OWNERSHIP,
            )
        except owned.OwnedFileError as error:
            plan.conflicts.append(str(error))
        else:
            if change.kind != "unchanged":
                plan.changes.append(change)

        self._plan_mcp(plan, canonical, target, directory / "config.toml", previous, force)
        return plan

    def _desired(self, plan, canonical, target) -> dict[str, dict[str, Any]]:
        desired = {
            _common.RESERVED_SERVER: {
                "command": sys.executable,
                "args": ["-m", cli.MCP_MODULE],
                "env_vars": list(STATE_ENV),
            },
        }
        for server in _common.selected_servers(canonical, target):
            if server.url is not None:
                plan.notes.append(
                    f"MCP server {server.name}: HTTP servers are not emitted for Codex yet "
                    "(no live evidence for header handling); skipped"
                )
                continue
            value: dict[str, Any] = {"command": _common.expand_home(server.command[0])}
            if len(server.command) > 1:
                value["args"] = list(server.command[1:])
            if server.env:
                value["env_vars"] = list(server.env)
            if server.cwd is not None:
                value["cwd"] = _common.expand_home(server.cwd)
            desired[server.name] = value
        return desired

    def _plan_mcp(self, plan, canonical, target, path: Path, previous, force):
        desired = self._desired(plan, canonical, target)
        try:
            text = owned.read_text(path) or ""
            current_tables = toml_tables.read_tables(text)
        except (owned.OwnedFileError, toml_tables.TomlTableError) as error:
            plan.conflicts.append(f"{path}: {error}; MCP servers not changed")
            self._keep_recorded(plan, previous)
            return
        names = sorted(set(desired) | self._managed_names(previous, "toml-table"))
        table_changes: dict[str, dict[str, Any] | None] = {}
        for name in names:
            entry_id = f"toml-table:{name}"
            recorded = previous.get(entry_id)
            raw = current_tables.get(name)
            current, disabled = _without_enabled(raw)
            want = desired.get(name)
            action = _common.server_action(
                want, current if name in current_tables else None,
                recorded.get("sha256") if recorded else None,
                adoptable=name == _common.RESERVED_SERVER and cli._is_owned_mcp(raw),
                force=force,
            )
            _common.report_action(plan, f"Codex MCP server {name} in {path}", action)
            entry = None if want is None else {
                "kind": "toml-table", "path": str(path), "key": name,
                "sha256": _common.digest_value(want),
            }
            if action in ("keep", "adopt"):
                plan.entries[entry_id] = entry
            elif action == "drifted" and recorded is not None:
                plan.entries[entry_id] = dict(recorded)
            elif action in ("add", "replace", "adopt-replace"):
                table_changes[name] = {**want, "enabled": False} if disabled else want
                plan.entries[entry_id] = entry
            elif action == "remove":
                table_changes[name] = None
        if not table_changes:
            return
        try:
            change = owned.plan_text_edit(
                path, lambda current: toml_tables.render_tables(current, table_changes)
            )
        except (owned.OwnedFileError, toml_tables.TomlTableError) as error:
            plan.conflicts.append(f"{path}: {error}; MCP servers not changed")
            for name in table_changes:
                plan.entries.pop(f"toml-table:{name}", None)
            self._keep_recorded(plan, previous)
            return
        if change.kind != "unchanged":
            plan.changes.append(change)

    @staticmethod
    def _keep_recorded(plan: _common.EmitPlan, previous) -> None:
        for entry_id, fields in previous.items():
            if fields.get("kind") == "toml-table":
                plan.entries.setdefault(entry_id, dict(fields))

    def capture(
        self, target: ProfileTarget, *, include_unmanaged: bool = False
    ) -> CanonicalFragment:
        directory = _common.config_dir(target)
        previous = owned.manifest_entries_for(target)
        warnings: list[str] = []
        skills = _common.capture_skill_tree(skills_dir(directory))
        instructions = _common.read_block(directory / "AGENTS.md")
        managed = self._managed_names(previous, "toml-table")
        servers = {}
        try:
            text = owned.read_text(directory / "config.toml") or ""
            tables = toml_tables.read_tables(text)
        except (owned.OwnedFileError, toml_tables.TomlTableError) as error:
            warnings.append(f"{directory / 'config.toml'}: {error}")
            tables = {}
        for name, table in sorted(tables.items()):
            if name == _common.RESERVED_SERVER or not isinstance(table, dict):
                continue
            if name not in managed and not include_unmanaged:
                continue
            label = f"Codex MCP server {name}"
            if "url" in table:
                servers[name] = {"url": table["url"]}
                continue
            command = table.get("command")
            args = table.get("args", [])
            if not isinstance(command, str) or not isinstance(args, list):
                warnings.append(f"{label}: unrecognized table; skipped")
                continue
            value: dict[str, Any] = {"command": [command, *map(str, args)]}
            names = [item for item in table.get("env_vars", []) if isinstance(item, str)]
            names += _common.env_names_from(table.get("env"), label, warnings)
            if names:
                value["env"] = sorted(set(names), key=names.index)
            if isinstance(table.get("cwd"), str):
                value["cwd"] = table["cwd"]
            servers[name] = value
        return _common.fragment(target, {
            "skills": skills,
            "instructions": instructions,
            "mcp_servers": servers,
            "warnings": warnings,
        })
