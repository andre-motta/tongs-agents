"""Validate the Pi emitter against a real Pi CLI (0.99.0 or newer) in a private root.

Run by the hosted ``pi-mcp-emitter`` job on the second Pi pin, and locally.
A fresh temporary root holds HOME, PI_CODING_AGENT_DIR, the XDG dirs and the
tongs-agents state dir. The emitter plans from the synthetic dotfiles in
tests/fixtures/dotfiles_synthetic and applies through owned.py; ``pi mcp add``
and ``remove`` and ``pi install`` of the packaged extension are the real Pi
CLI writing its own files. Only offline config commands run: no session, no
model, no provider, and ``pi mcp list`` (which starts servers) is not used.

Checks: the managed servers land in ``mcp.json`` next to a user server, a
foreign same-name server and an unknown top-level key, all kept; a second
plan is empty (idempotent); dropping the servers from the config removes
only ours, through the manifest. Evidence is redacted before it is printed.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path
from unittest.mock import patch

from tongs_agents import owned, pi_install
from tongs_agents.config_layer.emitters import EmitConfig, apply_plan
from tongs_agents.config_layer.emitters.pi import MCP_MINIMUM, PiEmitter
from tongs_agents.config_layer.protocol import ProfileTarget

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "dotfiles_synthetic"
SCHEMA = "pi-mcp-emitter-validation/1"
USER_SERVER = {"command": "user-mcp", "args": ["--serve"], "description": "kept"}
FOREIGN_NOTES = {"command": "someone-elses-notes"}


class Check(Exception):
    """A validation expectation that did not hold."""


def expect(condition, message):
    if not condition:
        raise Check(message)


def isolated_env(root):
    env = {key: os.environ[key] for key in ("LANG", "USER", "LOGNAME") if key in os.environ}
    path = [os.path.dirname(sys.executable), *os.environ.get("PATH", "").split(os.pathsep)]
    env.update({
        "PATH": os.pathsep.join(path),
        "HOME": str(root / "home"),
        "USERPROFILE": str(root / "home"),
        "PI_CODING_AGENT_DIR": str(root / "pi"),
        "PI_OFFLINE": "1",
        "PI_TELEMETRY": "0",
        "XDG_CONFIG_HOME": str(root / "xdg-config"),
        "XDG_STATE_HOME": str(root / "xdg-state"),
        "XDG_CACHE_HOME": str(root / "xdg-cache"),
        "XDG_DATA_HOME": str(root / "xdg-data"),
        "TONGS_AGENTS_STATE_DIR": str(root / "state"),
        "TMPDIR": str(root / "tmp"),
    })
    for name in ("home", "pi", "xdg-config", "xdg-state", "xdg-cache", "xdg-data", "tmp"):
        (root / name).mkdir(mode=0o700)
    return env


def servers(directory):
    try:
        return json.loads((directory / "mcp.json").read_text())
    except (OSError, ValueError):
        return {}


def summary(plan):
    return {
        "changes": sorted(change.kind for change in plan.changes if change.kind != "unchanged"),
        "commands": [step.argv[1:4] if step.argv else ["call"] for step in plan.commands],
        "notes": plan.notes,
        "conflicts": plan.conflicts,
    }


def exercise(root, pi_command, observations):
    directory = root / "pi"
    target = ProfileTarget("pi", directory)
    document = tomllib.loads((FIXTURE / "agentic.toml").read_text())
    profiles = {"pi-local": {"command": ["pi"], "config_dir": str(directory)}}
    config = EmitConfig.from_document(document, FIXTURE, profiles=profiles)
    version = pi_install.detect_pi_version(pi_command)
    observations["pi_version"] = None if version is None else ".".join(map(str, version))
    expect(version is not None and version >= MCP_MINIMUM,
           f"Pi {observations['pi_version']} is below the native MCP floor")
    (directory / "mcp.json").write_text(json.dumps({
        "userSetting": {"keep": True},
        "mcpServers": {"user-server": USER_SERVER, "notes": FOREIGN_NOTES},
    }, indent=4) + "\n")

    emitter = PiEmitter()
    first = emitter.plan_full(config, target)
    result = apply_plan(first)
    after_first = servers(directory)
    mcp = after_first.get("mcpServers", {})
    observations["first_apply"] = {
        **summary(first),
        "failed_steps": result.failed_steps,
        "servers": sorted(mcp),
        "user_server_kept": mcp.get("user-server") == USER_SERVER,
        "foreign_same_name_kept": mcp.get("notes") == FOREIGN_NOTES,
        "unknown_top_level_key_kept": after_first.get("userSetting") == {"keep": True},
        "issues_entry": mcp.get("issues"),
        "tongs_agents_args": (mcp.get("tongs-agents") or {}).get("args"),
        "extension_config_written": (directory / pi_install.PI_CONFIG_NAME).exists(),
        "secret_values_written": False,
    }
    expect(not result.failed_steps, f"a Pi CLI step failed: {result.failed_steps}")
    expect(set(mcp) == {"user-server", "notes", "issues", "tongs-agents"},
           f"unexpected servers after apply: {sorted(mcp)}")
    expect(mcp["issues"].get("command") == "example-issues-mcp"
           and mcp["issues"].get("args") == ["--stdio"] and not mcp["issues"].get("env"),
           f"issues was not written as planned: {mcp['issues']}")
    expect(observations["first_apply"]["user_server_kept"], "the user server changed")
    expect(observations["first_apply"]["foreign_same_name_kept"], "the foreign server changed")
    expect(observations["first_apply"]["unknown_top_level_key_kept"], "a user key was lost")
    expect(any("notes" in item for item in first.conflicts), "no conflict for notes")

    before = {path: path.read_bytes() for path in directory.rglob("*") if path.is_file()}
    second = emitter.plan_full(config, target)
    second_result = apply_plan(second)
    after = {path: path.read_bytes() for path in directory.rglob("*") if path.is_file()}
    observations["second_apply"] = {
        **summary(second),
        "failed_steps": second_result.failed_steps,
        "files_changed": sorted(str(path.relative_to(directory))
                                for path in after if before.get(path) != after[path]),
    }
    expect(not observations["second_apply"]["changes"] and not second.commands,
           "the second plan was not empty")
    expect(not observations["second_apply"]["files_changed"], "the second apply wrote bytes")

    dropped = EmitConfig.from_document({**document, "mcp_servers": {}}, FIXTURE,
                                       profiles=profiles)
    third = emitter.plan_full(dropped, target)
    third_result = apply_plan(third)
    mcp = servers(directory).get("mcpServers", {})
    observations["remove_through_manifest"] = {
        **summary(third),
        "failed_steps": third_result.failed_steps,
        "servers": sorted(mcp),
    }
    expect(set(mcp) == {"user-server", "notes", "tongs-agents"},
           f"unexpected servers after removal: {sorted(mcp)}")
    observations["manifest_entry_kinds"] = sorted(
        {fields["kind"] for fields in owned.manifest_entries_for(target).values()})


def redact(text, replacements):
    for value, label in sorted(replacements.items(), key=lambda item: -len(item[0])):
        if value:
            text = text.replace(value, label)
    return text


def validate(output=None):
    pi_command = shutil.which("pi")
    if pi_command is None:
        raise RuntimeError("pi is not on PATH")
    secret = "pi-emitter-synthetic-secret"
    root = Path(tempfile.mkdtemp(prefix="tongs-pi-mcp-"))
    observations = {}
    started = time.monotonic()
    real_home = str(Path.home())
    try:
        env = isolated_env(root)
        env["EXAMPLE_ISSUES_TOKEN"] = secret
        with patch.dict(os.environ, env, clear=True):
            exercise(root, pi_command, observations)
            written = b"".join(path.read_bytes() for path in root.rglob("*")
                               if path.is_file() and not path.is_symlink())
            observations.setdefault("first_apply", {})["secret_values_written"] = (
                secret.encode() in written)
            expect(not observations["first_apply"]["secret_values_written"],
                   "an environment value was written")
        error = None
    except Exception as failure:
        error = f"{type(failure).__name__}: {failure}"
    finally:
        shutil.rmtree(root, ignore_errors=True)
    commit = subprocess.run(["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
                            capture_output=True, text=True)
    evidence = {
        "schema": SCHEMA,
        "result": "pass" if error is None else "fail",
        "error": error,
        "provenance": "live",
        "provenance_detail": (
            "the real Pi CLI ran offline config commands (pi --version, pi mcp add, pi mcp "
            "remove, pi install of the packaged extension) in a private root; no session, "
            "model or provider; servers were never started"),
        "fixture": "tests/fixtures/dotfiles_synthetic (synthetic names only)",
        **observations,
        "temp_dir_removed": not root.exists(),
        "duration_seconds": round(time.monotonic() - started, 1),
        "tested_commit": commit.stdout.strip() if commit.returncode == 0 else None,
        "platform": sys.platform,
    }
    text = redact(json.dumps(evidence, indent=2) + "\n", {
        str(root): "<tmp>", sys.executable: "<python>", str(REPO_ROOT): "<repo>",
        real_home: "<home>",
    })
    if "/home/" in text or "/tmp/" in text:
        raise RuntimeError("refusing to print evidence with an absolute path")
    if output:
        Path(output).write_text(text, encoding="utf-8")
    print(text, end="")
    return evidence


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", help="also write the evidence to this file")
    arguments = parser.parse_args(argv)
    evidence = validate(arguments.out)
    return 0 if evidence["result"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
