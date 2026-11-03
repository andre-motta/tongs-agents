"""Pi emitter (docs/work/emitter-strategy.md, rows 9 to 12).

- Skills: owned dir ``<pi>/skills/tongs-agents/<skill>/``, copied.
- Instructions: a managed block in ``<pi>/AGENTS.md``.
- MCP: ``pi mcp add`` and ``remove`` with ``PI_CODING_AGENT_DIR`` per target,
  only on Pi 0.99.0 or newer (native MCP); below it the servers are skipped
  with one line. No ``env`` is written: Pi stdio servers inherit its
  environment.
- Hooks: the packaged extension plus the owned ``<pi>/tongs-agents.json``,
  installed by ``pi_install.install_pi`` when they are missing or stale.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Any

from tongs_agents import cli, owned, pi_install
from tongs_agents.config_layer.emitters import _common
from tongs_agents.config_layer.emitters.base import BaseEmitter
from tongs_agents.config_layer.emitters.claude import skill_tree_files
from tongs_agents.config_layer.protocol import CanonicalFragment, ProfileTarget

MCP_MINIMUM = (0, 99, 0)


def skills_dir(directory: Path) -> Path:
    return directory / "skills" / _common.BUNDLE


def normalize(entry: Any) -> tuple[Any, bool]:
    """The fields tongs-agents sets on a Pi MCP entry, and whether it is disabled."""
    if not isinstance(entry, dict):
        return {"unrecognized": entry}, False
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
    return result, entry.get("enabled") is False


def _version_text(version: tuple[int, ...]) -> str:
    return ".".join(str(part) for part in version)


def _version_problem(minimum: tuple[int, ...], version: tuple[int, ...] | None) -> str:
    found = "an unrecognized version" if version is None else _version_text(version)
    return f"Pi {_version_text(minimum)} or newer is required (found {found})"


def _blocked(plan: _common.EmitPlan, path: Path, key: str, reason: str) -> None:
    """An entry Pi cannot take yet: pending with the reason, so status is not clean."""
    plan.notes.append(f"Pi monitoring skipped: {reason}")
    plan.items.append(_common.Item(path, key, _common.PENDING, reason))


class PiEmitter(BaseEmitter):
    harness_id = "pi"

    def plan_full(
        self, config: Any, target: ProfileTarget, *, force: bool = False
    ) -> _common.EmitPlan:
        canonical, plan, previous = self._start(config, target)
        directory = _common.config_dir(target)
        pi_command = shutil.which("pi")
        if pi_command is None and _common.skip_absent_home(plan, "pi", directory):
            return plan

        skills = _common.selected_skills(canonical, target)
        files = skill_tree_files(skills_dir(directory), skills)
        _common.plan_owned_tree(plan, files, previous=previous, force=force)

        body = _common.render_instructions(canonical, "pi")
        agents_md = directory / "AGENTS.md"
        if body is not None or f"md-block:{agents_md}" in previous:
            _common.plan_markdown_block(plan, agents_md, body, previous=previous, force=force)

        version = None if pi_command is None else pi_install.detect_pi_version(pi_command)
        self._plan_hooks(plan, directory, pi_command, version)
        self._plan_mcp(plan, canonical, target, directory, previous, force,
                       pi_command, version)
        return plan

    def _plan_hooks(self, plan, directory: Path, pi_command, version) -> None:
        hook = _common.hook_command()
        config_path = directory / pi_install.PI_CONFIG_NAME
        if pi_command is None:
            plan.items.append(_common.Item(
                config_path, "Pi extension", _common.SKIPPED, _common.not_on_path("pi"),
            ))
            return
        if not Path(hook).is_absolute():
            _blocked(plan, config_path, "Pi extension", cli.PI_PREREQUISITE)
            return
        if version is None or version < pi_install.MINIMUM_PI_VERSION:
            _blocked(plan, config_path, "Pi extension",
                     _version_problem(pi_install.MINIMUM_PI_VERSION, version))
            return
        try:
            current = pi_install._read_owned_config(config_path)
            expected_hook = str(Path(hook).resolve(strict=True))
            extension = str(pi_install.pi_extension_path())
        except ValueError as error:
            plan.conflict(config_path, str(error))
            return
        except OSError as error:
            _blocked(plan, config_path, "Pi extension", str(error))
            return
        if (
            current is not None
            and current.get("hook_path") == expected_hook
            and current.get("extension_path") == extension
            and current.get("managed_paths") == [extension]
        ):
            plan.items.append(_common.Item(config_path, "Pi extension", _common.IN_SYNC))
            return
        environment = pi_install.pi_environment(directory)
        plan.items.append(_common.Item(config_path, "Pi extension", _common.PENDING))
        plan.commands.append(_common.CommandStep(
            description=f"install the tongs-agents Pi extension into {directory}",
            call=lambda: pi_install.install_pi(pi_command, hook, environment),
        ))

    def _desired(self, plan, canonical, target) -> dict[str, dict[str, Any]]:
        desired = {
            _common.RESERVED_SERVER: {
                "command": sys.executable, "args": ["-m", cli.MCP_MODULE],
            },
        }
        for server in _common.selected_servers(canonical, target):
            if server.url is not None:
                plan.notes.append(
                    f"MCP server {server.name}: HTTP servers are not emitted for Pi yet "
                    "(no live evidence for header handling); skipped"
                )
                continue
            value: dict[str, Any] = {
                "command": _common.expand_home(server.command[0]),
                "args": list(server.command[1:]),
            }
            if server.cwd is not None:
                value["cwd"] = _common.expand_home(server.cwd)
            desired[server.name] = value
        return desired

    def _plan_mcp(self, plan, canonical, target, directory, previous, force,
                  pi_command, version) -> None:
        managed = self._managed_names(previous, "mcp-cli")
        keep_recorded = [
            (entry_id, fields) for entry_id, fields in previous.items()
            if fields.get("kind") == "mcp-cli"
        ]
        if pi_command is None:
            names = set(self._desired(plan, canonical, target)) | managed
            _common.skip_cli_entries(plan, "pi", directory / "mcp.json", names)
            return
        if version is None or version < MCP_MINIMUM:
            reason = _version_problem(MCP_MINIMUM, version)
            plan.notes.append(f"Pi MCP servers skipped: {reason}")
            for name in sorted(set(self._desired(plan, canonical, target)) | managed):
                plan.items.append(_common.Item(
                    directory / "mcp.json", f"mcpServers.{name}", _common.PENDING, reason,
                ))
            plan.entries.update((key, dict(value)) for key, value in keep_recorded)
            return
        desired = self._desired(plan, canonical, target)
        path = directory / "mcp.json"
        try:
            current_raw = _common.read_mcp_servers(path)
        except owned.OwnedFileError as error:
            plan.conflict(path, f"{error}; MCP servers not changed")
            plan.entries.update((key, dict(value)) for key, value in keep_recorded)
            return
        env = {"PI_CODING_AGENT_DIR": str(directory), "PI_OFFLINE": "1"}
        for name in sorted(set(desired) | managed):
            entry_id = f"mcp:{name}"
            recorded = previous.get(entry_id)
            raw = current_raw.get(name)
            current, disabled = normalize(raw) if name in current_raw else (None, False)
            want = desired.get(name)
            action = _common.decide_server(
                plan, path, want, current, recorded, adoptable=False, force=force,
            )
            label = f"Pi MCP server {name}"
            _common.report_action(plan, label, action, path=path, key=f"mcpServers.{name}",
                                  recorded=_common.is_recorded(recorded))
            entry = None if want is None else {
                "kind": "mcp-cli", "key": name, "sha256": _common.digest_value(want),
            }
            if action == "keep":
                plan.entries[entry_id] = entry
                continue
            if action == "drifted" and recorded is not None:
                plan.entries[entry_id] = dict(recorded)
                continue
            if disabled and action in ("replace", "remove"):
                plan.notes.append(f"{label}: disabled in {path}; left as is")
                if recorded is not None:
                    plan.entries[entry_id] = dict(recorded)
                continue
            if action == "remove":
                plan.commands.append(_common.CommandStep(
                    description=f"remove {label}",
                    argv=(pi_command, "mcp", "remove", name),
                    env=env, entry_id=entry_id, entry=None,
                ))
            elif action in ("add", "replace"):
                # `pi mcp add` replaces an existing name in place (Pi 1.5.0
                # dist/extensions/mcp/cli.js: "Add or replace a server in mcp.json",
                # and the live probe in docs/work/emitter-strategy.md), so a
                # replace is one add, unlike Claude Code's remove then add.
                cwd = ("--cwd", want["cwd"]) if "cwd" in want else ()
                plan.commands.append(_common.CommandStep(
                    description=f"add {label}",
                    argv=(pi_command, "mcp", "add", name, *cwd, "--",
                          want["command"], *want["args"]),
                    env=env, entry_id=entry_id, entry=entry,
                ))
                plan.entries[entry_id] = entry

    def capture(
        self, target: ProfileTarget, *, include_unmanaged: bool = False
    ) -> CanonicalFragment:
        directory = _common.config_dir(target)
        previous = owned.manifest_entries_for(target)
        warnings: list[str] = []
        skills = _common.capture_skill_tree(skills_dir(directory))
        instructions = _common.read_block(directory / "AGENTS.md")
        managed = self._managed_names(previous, "mcp-cli")
        try:
            entries = _common.read_mcp_servers(directory / "mcp.json")
        except owned.OwnedFileError as error:
            warnings.append(str(error))
            entries = {}
        servers = {}
        for name, entry in sorted(entries.items()):
            if name == _common.RESERVED_SERVER or not isinstance(entry, dict):
                continue
            if name not in managed and not include_unmanaged:
                continue
            label = f"Pi MCP server {name}"
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
            if isinstance(entry.get("cwd"), str):
                value["cwd"] = entry["cwd"]
            servers[name] = value
        return _common.fragment(target, {
            "skills": skills,
            "instructions": instructions,
            "mcp_servers": servers,
            "warnings": warnings,
        })
