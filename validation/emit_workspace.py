"""Live acceptance for the Claude emitter: an emitted profile config dir shows hook status.

Runs validation/workspace.py unchanged except for one step: instead of
``tongs-agents install --claude-config-dir``, the Claude emitter plans from
the synthetic dotfiles in tests/fixtures/dotfiles_synthetic and applies
through owned.py into the hooked profile's own CLAUDE_CONFIG_DIR. The rest is
the workspace validator's real run: an isolated Zellij server, the
``tongs-agents workspace`` launcher and sidebar, and fake harness fixtures
(validation/fixtures/fake_agent.py) that run the hook commands registered in
their config dir. The Claude fixture's row must carry hook status, not
``discovered``; claude on the private PATH is the recording stub, so
``claude mcp add`` is recorded, never run.

Linux with zellij on PATH. Writes validation/evidence/emit-workspace-linux-x86_64.json.
"""

import argparse
import os
import sys
import tomllib
from pathlib import Path

import workspace
from workspace import cli_calls, expect, read_json

from tongs_agents.config_layer.emitters import EMITTERS, EmitConfig, apply_plan
from tongs_agents.config_layer.protocol import ProfileTarget

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "dotfiles_synthetic"
EVIDENCE = REPO_ROOT / "validation" / "evidence" / "emit-workspace-linux-x86_64.json"
EMITTER_FILES = (
    "src/tongs_agents/config_layer/emitters/_common.py",
    "src/tongs_agents/config_layer/emitters/base.py",
    "src/tongs_agents/config_layer/emitters/claude.py",
    "src/tongs_agents/owned.py",
    "tests/fixtures/dotfiles_synthetic/agentic.toml",
    "validation/emit_workspace.py",
)


def emit_install(root, env, tongs_bin, observations):
    """Stand-in for workspace.install: emit the hooked profile's config dir."""
    root = Path(root)
    hooked = root / "claude-hooked"
    target = ProfileTarget("claude", hooked)
    document = tomllib.loads((FIXTURE / "agentic.toml").read_text())
    config = EmitConfig.from_document(document, FIXTURE, profiles={
        workspace.HOOKED_PROFILE: {"command": ["claude"], "config_dir": str(hooked)}})
    plan = EMITTERS["claude"].plan_full(config, target)
    result = apply_plan(plan)
    second = EMITTERS["claude"].plan_full(config, target)
    settings = read_json(hooked / "settings.json") or {}
    commands = [hook.get("command", "") for groups in (settings.get("hooks") or {}).values()
                for group in groups for hook in group.get("hooks", [])]
    events = sorted(settings.get("hooks") or {})
    mcp = [call for call in cli_calls(root)
           if call["cli"] == "claude" and call["args"][:2] == ["mcp", "add"]]
    observations["install"] = {
        "via": "Claude emitter plan_full and apply_plan (no tongs-agents install)",
        "conflicts": plan.conflicts,
        "failed_steps": result.failed_steps,
        "files": sorted(str(path.relative_to(hooked)) for path in hooked.rglob("*")
                        if path.is_file()),
        "hook_events": events,
        "hook_commands_are_owned": bool(commands) and all(
            os.path.basename(command.split()[0]) == "tongs-agents-hook"
            for command in commands),
        "private_home_settings_written": (root / "home" / ".claude" / "settings.json").exists(),
        "bare_config_dir_untouched": not any((root / "claude-bare").iterdir()),
        "claude_mcp_add_calls": [{"name": call["args"][4],
                                  "claude_config_dir_is_target":
                                      call["env"].get("CLAUDE_CONFIG_DIR") == str(hooked)}
                                 for call in mcp],
        "second_plan_file_changes": sorted(change.kind for change in second.changes
                                           if change.kind != "unchanged"),
    }
    expect(not plan.conflicts and not result.failed_steps, "the emitter reported problems")
    expect("SessionStart" in events and "Stop" in events, "the emitter wrote no hook groups")
    expect(observations["install"]["hook_commands_are_owned"], "foreign hook command written")
    expect(not observations["install"]["private_home_settings_written"],
           "the emitter touched the default Claude config dir")
    expect(observations["install"]["bare_config_dir_untouched"],
           "the emitter touched a config dir it was not given")
    expect(mcp and all(call["claude_config_dir_is_target"]
                       for call in observations["install"]["claude_mcp_add_calls"]),
           "MCP registration did not target the config dir")
    expect(not observations["install"]["second_plan_file_changes"],
           "a second plan still changes files")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--no-write", action="store_true", help="print evidence only")
    parser.add_argument("--out", default=str(EVIDENCE))
    arguments = parser.parse_args(argv)
    workspace.install = emit_install
    workspace.HASHED = (*workspace.HASHED, *EMITTER_FILES)
    evidence = workspace.validate(write=False)
    evidence["provenance_detail"] = (
        "real zellij server and PTY clients, real tongs-agents workspace, sidebar, launcher "
        "and hook in a private root; the hooked profile's config dir was written by the "
        "Claude emitter instead of tongs-agents install; fake harness fixtures stand in "
        "for agents and send hook events")
    if not arguments.no_write:
        workspace.write_evidence(Path(arguments.out), evidence)
    return 0 if evidence["result"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
