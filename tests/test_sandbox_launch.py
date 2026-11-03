"""Sandboxed launches (rows 4, 5, 8, 12, 16, 20): exact argv, reuse, Ready, spool hooks."""

import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_sandbox_support import (  # noqa: F401
    create_sandbox,
    fake,
    posix_only,
)
from tongs_agents import launch, launches, paths
from tongs_agents.config_layer.emitters import sandbox as sandbox_emitter
from tongs_agents.profiles import parse_profile
from tongs_agents.sandbox import launcher, state
from tongs_agents.sandbox.launcher import SandboxLaunchError
from tongs_agents.sandbox.openshell import OpenShell

NODE = shutil.which("node")


@pytest.fixture
def project(tmp_path):
    directory = tmp_path / "project"
    directory.mkdir()
    (directory / "README.md").write_text("hello\n")
    return directory


def sandboxed(project, harness_command=("claude",), **sandbox):
    return parse_profile("boxed", {
        "command": list(harness_command), "cwd": str(project), "sandbox": sandbox,
    })


def pending(profile):
    return launches.create_pending(profile, "agents", "zellij")


def no_wait(client, name, out):
    return client.get(name)


@posix_only
def test_create_argv_is_exact_and_has_no_secret_or_auto_approval(fake, project):
    profile = sandboxed(project, image="example/claude:1", providers=["anthropic"],
                        policy="/home/user/policy.yaml", cpu="2", memory="4Gi")
    record = pending(profile)
    out = io.StringIO()
    prepared = launcher.prepare(profile, record, dict(os.environ), ["claude"],
                                out=out, wait=no_wait)
    create = next(call for call in fake.calls() if call[:2] == ["sandbox", "create"])
    stage = create[create.index("--upload", create.index("--upload") + 1) + 1]
    assert create == [
        "sandbox", "create", "--name", "tongs-boxed", "--detach",
        "--label", f"tongs-agents.launch={record.launch_id}",
        "--label", "tongs-agents.profile=boxed",
        "--from", "example/claude:1", "--provider", "anthropic",
        "--policy", "/home/user/policy.yaml", "--cpu", "2", "--memory", "4Gi",
        "--upload", ".:/sandbox/workspace",
        "--upload", stage,
    ]
    assert stage.endswith("/.tongs-agents:/sandbox/workspace")
    [create_call] = [call for call in fake.raw_calls() if call["argv"][:2] == ["sandbox",
                                                                                "create"]]
    assert create_call["cwd"] == str(project)
    flat = " ".join(" ".join(call) for call in fake.calls())
    for forbidden in ("--approval-mode", "approve-all", "--global", "--env"):
        assert forbidden not in flat
    assert prepared.reused is False
    assert "creating sandbox tongs-boxed" in out.getvalue()


@posix_only
def test_exec_pane_argv_and_uploaded_config(fake, project):
    profile = sandboxed(project, ("claude", "--model", "x"))
    record = pending(profile)
    prepared = launcher.prepare(profile, record, {**os.environ, "KEEP": "1"},
                                ["claude", "--model", "x"], out=io.StringIO(), wait=no_wait)
    launch_id = record.launch_id
    base = "/sandbox/workspace/.tongs-agents"
    assert prepared.argv == [
        str(Path(fake.executable)), "sandbox", "exec", "-n", "tongs-boxed", "--tty",
        "--workdir", "/sandbox/workspace",
        "--env", f"TONGS_AGENTS_LAUNCH_ID={launch_id}",
        "--env", "TONGS_AGENTS_PROFILE=boxed",
        "--env", f"TONGS_AGENTS_SPOOL={base}/spool/{launch_id}",
        "--env", f"CLAUDE_CONFIG_DIR={base}/config/claude",
        "--", "claude", "--model", "x",
    ]
    assert prepared.env["KEEP"] == "1"
    assert prepared.env["TONGS_AGENTS_LAUNCH_ID"] == launch_id
    assert (fake.fs("tongs-boxed", "/sandbox/workspace/README.md")).read_text() == "hello\n"
    assert not fake.fs("tongs-boxed", f"/sandbox/workspace/{project.name}").exists()
    assert not fake.fs("tongs-boxed", f"{base}/.tongs-agents").exists()
    settings = json.loads(fake.fs("tongs-boxed", f"{base}/config/claude/settings.json")
                          .read_text())
    assert set(settings) == {"hooks"}
    assert "SessionStart" in settings["hooks"]
    recorded = state.get(launch_id)
    assert (recorded.sandbox, recorded.spool, recorded.cwd) == (
        "tongs-boxed", f"{base}/spool/{launch_id}", str(project)
    )


@posix_only
def test_relaunch_reuses_the_labelled_sandbox_without_uploading_the_workspace(fake, project):
    profile = sandboxed(project)
    launcher.prepare(profile, pending(profile), dict(os.environ), ["claude"],
                     out=io.StringIO(), wait=no_wait)
    work = fake.fs("tongs-boxed", "/sandbox/workspace/README.md")
    work.write_text("changed by the agent\n")
    out = io.StringIO()
    second = pending(profile)
    prepared = launcher.prepare(profile, second, dict(os.environ), ["claude"], out=out,
                                wait=no_wait)
    assert prepared.reused is True
    assert "reusing sandbox tongs-boxed" in out.getvalue()
    assert work.read_text() == "changed by the agent\n"
    creates = [call for call in fake.calls() if call[:2] == ["sandbox", "create"]]
    uploads = [call for call in fake.calls() if call[:2] == ["sandbox", "upload"]]
    assert len(creates) == 1
    assert len(uploads) == 1
    assert uploads[0][2] == "tongs-boxed"
    assert uploads[0][3].endswith("/.tongs-agents")
    assert uploads[0][4:] == ["/sandbox/workspace", "--no-git-ignore"]
    assert fake.fs("tongs-boxed", "/sandbox/workspace/.tongs-agents/config/claude/"
                   "settings.json").exists()
    assert not fake.fs("tongs-boxed", "/sandbox/workspace/.tongs-agents/.tongs-agents").exists()
    assert f"spool/{second.launch_id}" in " ".join(prepared.argv)


@posix_only
@pytest.mark.parametrize("workdir", ["/sandbox", "/sandbox/deep/project"])
def test_uploads_land_where_the_agent_and_its_config_dir_point(fake, project, workdir):
    profile = sandboxed(project, ("codex",), workdir=workdir)
    record = pending(profile)
    prepared = launcher.prepare(profile, record, dict(os.environ), ["codex"],
                                out=io.StringIO(), wait=no_wait)
    argv = prepared.argv
    workdir_arg = argv[argv.index("--workdir") + 1]
    config_dir = next(item.split("=", 1)[1] for item in argv if item.startswith("CODEX_HOME="))
    assert workdir_arg == workdir
    assert fake.fs("tongs-boxed", f"{workdir_arg}/README.md").read_text() == "hello\n"
    assert fake.fs("tongs-boxed", f"{config_dir}/hooks.json").is_file()
    launcher.prepare(profile, pending(profile), dict(os.environ), ["codex"],
                     out=io.StringIO(), wait=no_wait)
    assert fake.fs("tongs-boxed", f"{config_dir}/hooks.json").is_file()
    assert not fake.fs("tongs-boxed", f"{workdir}/.tongs-agents/.tongs-agents").exists()


def test_upload_parent_needs_a_source_named_after_its_destination(tmp_path):
    stage = tmp_path / ".tongs-agents"
    assert launcher.upload_parent(stage, "/sandbox/w/.tongs-agents") == "/sandbox/w"
    with pytest.raises(SandboxLaunchError, match="names differ"):
        launcher.upload_parent(tmp_path / "stage", "/sandbox/w/.tongs-agents")
    with pytest.raises(SandboxLaunchError, match="holds ':'"):
        launcher.upload_parent(tmp_path / "a:b" / ".tongs-agents", "/sandbox/.tongs-agents")


@posix_only
def test_a_missing_cwd_fails_before_anything_is_created(fake, tmp_path):
    profile = sandboxed(tmp_path / "gone")
    with pytest.raises(SandboxLaunchError, match="not a directory"):
        launcher.prepare(profile, pending(profile), dict(os.environ), ["claude"],
                         out=io.StringIO(), wait=no_wait)
    assert not [call for call in fake.calls() if call[:2] == ["sandbox", "create"]]


@posix_only
def test_an_unlabelled_sandbox_of_the_same_name_is_never_reused(fake, project):
    create_sandbox(fake, "tongs-boxed", profile="someone-else")
    profile = sandboxed(project)
    with pytest.raises(SandboxLaunchError, match="without the tongs-agents.profile=boxed"):
        launcher.prepare(profile, pending(profile), dict(os.environ), ["claude"],
                         out=io.StringIO(), wait=no_wait)
    mutating = [call for call in fake.calls() if call[:2] in (["sandbox", "create"],
                                                               ["sandbox", "upload"])]
    assert len(mutating) == 1  # the setup's own create
    assert fake.state()["sandboxes"]["tongs-boxed"]["labels"] == {
        "tongs-agents.profile": "someone-else"
    }


@posix_only
def test_waits_visibly_for_provisioning_then_ready(fake, project, monkeypatch):
    monkeypatch.setenv("FAKE_OPENSHELL_PHASES", "Provisioning,Provisioning,Ready")
    profile = sandboxed(project)
    out = io.StringIO()
    slept = []
    launcher.prepare(profile, pending(profile), dict(os.environ), ["claude"], out=out,
                     wait=lambda client, name, stream: launcher.wait_ready(
                         client, name, stream, sleep=slept.append))
    assert out.getvalue().count("waiting for sandbox tongs-boxed (Provisioning)") == 1
    assert len(slept) == 2


@posix_only
@pytest.mark.parametrize("phase", ["Error", "Stopped", "Completed"])
def test_a_not_ready_phase_fails_in_the_pane(fake, project, monkeypatch, phase):
    monkeypatch.setenv("FAKE_OPENSHELL_PHASES", phase)
    profile = sandboxed(project)
    with pytest.raises(SandboxLaunchError, match=f"not ready \\(phase {phase}\\)"):
        launcher.prepare(profile, pending(profile), dict(os.environ), ["claude"],
                         out=io.StringIO())
    assert state.list_records() == []


@posix_only
def test_waiting_has_a_deadline(fake, project, monkeypatch):
    create_sandbox(fake, "box", phases="Provisioning")
    ticks = iter(range(0, 1000, 100))
    with pytest.raises(SandboxLaunchError, match="not ready after 180s"):
        launcher.wait_ready(OpenShell(), "box", io.StringIO(), clock=lambda: next(ticks),
                            sleep=lambda seconds: None)


@posix_only
def test_policy_advisor_is_set_on_this_sandbox_only(fake, project):
    profile = sandboxed(project, policy_advisor=True)
    launcher.prepare(profile, pending(profile), dict(os.environ), ["claude"],
                     out=io.StringIO(), wait=no_wait)
    settings = [call for call in fake.calls() if call[0] == "settings"]
    assert settings == [["settings", "set", "tongs-boxed", "--key",
                         "agent_policy_proposals_enabled", "--value", "true"]]


@posix_only
def test_attach_mode_runs_the_agent_as_main_process_and_connects(fake, tmp_path):
    profile = parse_profile("boxed", {
        "command": ["pi"], "sandbox": {"mode": "attach", "image": "example/pi:1"},
    })
    record = pending(profile)
    out = io.StringIO()
    prepared = launcher.prepare(profile, record, dict(os.environ), ["pi"], out=out,
                                wait=no_wait)
    create = next(call for call in fake.calls() if call[:2] == ["sandbox", "create"])
    assert "--upload" not in create
    assert create[-4:] == [
        "--env", "TONGS_AGENTS_SPOOL=/sandbox/workspace/.tongs-agents/spool/main", "--", "pi",
    ]
    assert prepared.argv == [fake.executable, "sandbox", "connect", "tongs-boxed"]
    assert "Ctrl-D as detach" in out.getvalue()
    assert state.get(record.launch_id).mode == "attach"


@posix_only
def test_old_cli_is_refused_before_anything_is_created(fake, project, monkeypatch):
    monkeypatch.setenv("FAKE_OPENSHELL_VERSION", "0.0.3")
    profile = sandboxed(project)
    with pytest.raises(SandboxLaunchError, match="older than 0.1.0"):
        launcher.prepare(profile, pending(profile), dict(os.environ), ["claude"],
                         out=io.StringIO())
    assert fake.calls() == [["--version"]]


def test_sandbox_name_is_derived_from_the_profile():
    profile = parse_profile("my_agent_", {"command": ["claude"], "cwd": "/p", "sandbox": {}})
    assert launcher.sandbox_name(profile) == "tongs-my-agent"
    named = parse_profile("x", {"command": ["claude"], "cwd": "/p", "sandbox": {"name": "box"}})
    assert launcher.sandbox_name(named) == "box"


# The launcher entry point


@pytest.fixture
def config(tmp_path, monkeypatch):
    directory = tmp_path / "config"
    directory.mkdir(exist_ok=True)
    monkeypatch.setenv(paths.CONFIG_DIR_ENV, str(directory))

    def write(text):
        path = directory / "config.toml"
        path.write_text(text)
        path.chmod(0o600)

    return write


@posix_only
def test_launch_main_execs_openshell_for_a_sandboxed_profile(fake, config, project,
                                                              monkeypatch):
    config(f'[profiles.boxed]\ncommand = ["codex"]\ncwd = "{project}"\n'
           "[profiles.boxed.sandbox]\n")
    profile = parse_profile("boxed", {"command": ["codex"], "cwd": str(project),
                                      "sandbox": {}})
    record = pending(profile)
    seen = []

    def fake_execvpe(file, args, env):
        seen.append((file, list(args), dict(env)))
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(launch.os, "execvpe", fake_execvpe)
    assert launch.main(["boxed", "--launch-id", record.launch_id]) == launch.EXIT_EXEC_FAILED
    file, args, env = seen[0]
    assert file == fake.executable
    assert args[args.index("--") + 1:] == ["codex", "--no-daemon"]
    assert "CODEX_HOME=/sandbox/workspace/.tongs-agents/config/codex" in args
    assert env["TONGS_AGENTS_LAUNCH_ID"] == record.launch_id
    assert launches.get(record.launch_id).state == "running"


@posix_only
def test_launch_main_shows_a_not_ready_error(fake, config, project, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_OPENSHELL_PHASES", "Error")
    config(f'[profiles.boxed]\ncommand = ["claude"]\ncwd = "{project}"\n'
           "[profiles.boxed.sandbox]\n")
    record = pending(parse_profile("boxed", {"command": ["claude"], "cwd": str(project),
                                             "sandbox": {}}))
    monkeypatch.setattr(launch.os, "execvpe", lambda *args: pytest.fail("must not exec"))
    assert launch.main(["boxed", "--launch-id", record.launch_id]) == launch.EXIT_SANDBOX
    assert "is not ready (phase Error)" in capsys.readouterr().err


# The sandbox flavour of the emitters


def test_sandbox_trees_emit_spool_hooks_and_no_mcp():
    claude = sandbox_emitter.sandbox_files("claude")
    codex = sandbox_emitter.sandbox_files("codex")
    pi = sandbox_emitter.sandbox_files("pi")
    assert set(claude) == {"settings.json"}
    assert set(codex) == {"hooks.json"}
    assert set(pi) == {"extensions/tongs-agents.js"}
    assert sandbox_emitter.sandbox_files("other") == {}
    for tree in (claude, codex):
        text = next(iter(tree.values())).decode()
        assert "tongs-agents-hook" not in text
        assert "mcp" not in text.lower()
        commands = [hook["command"] for groups in json.loads(text)["hooks"].values()
                    for group in groups for hook in group["hooks"]]
        assert all(command.startswith("sh -c ") for command in commands)
    hooks = json.loads(codex["hooks.json"])["hooks"]
    assert hooks["SessionStart"][0]["matcher"] == "startup|resume|clear"


def test_sandbox_tree_copies_scoped_skills_and_instructions(tmp_path):
    skill = tmp_path / "skills" / "review"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: review\ndescription: d\n---\nBody\n")
    (tmp_path / "AGENTS.md").write_text("Be kind.\n")

    class Canonical:
        def skills_for(self, harness, names):
            assert names == ("boxed",)
            return [type("S", (), {"name": "review", "path": skill})()]

        def instructions_for(self, harness):
            return "Be kind.\n"

    tree = sandbox_emitter.sandbox_files("claude", config=Canonical(), profile="boxed")
    assert "skills/tongs-agents/skills/review/SKILL.md" in tree
    assert tree["CLAUDE.md"] == b"@tongs-agents/instructions.md\n"
    assert tree["tongs-agents/instructions.md"] == b"Be kind.\n"


def test_write_tree_refuses_escaping_paths(tmp_path):
    with pytest.raises(ValueError, match="refusing"):
        sandbox_emitter.write_tree({"../x": b"1"}, tmp_path)


@posix_only
@pytest.mark.parametrize("event", ["session-start", "tool-use"])
def test_spool_hook_command_spools_the_raw_payload(tmp_path, event):
    spool = tmp_path / "spool dir"
    payload = json.dumps({"session_id": "s1", "cwd": "/sandbox/w", "tool_name": "Bash"})
    result = subprocess.run(sandbox_emitter.spool_hook_command(event), shell=True,
                            input=payload, text=True, capture_output=True, timeout=30,
                            env={**os.environ, "TONGS_AGENTS_SPOOL": str(spool)})
    assert result.returncode == 0, result.stderr
    files = list(spool.iterdir())
    assert len(files) == 1
    name = files[0].name
    assert name.endswith(f"-{event}.json")
    assert len(name.split("-", 1)[0]) == 19
    assert files[0].read_text() == payload
    assert oct(spool.stat().st_mode & 0o777) == oct(0o700)


@posix_only
def test_spool_hook_command_fails_open_without_a_spool(tmp_path):
    env = {key: value for key, value in os.environ.items() if key != "TONGS_AGENTS_SPOOL"}
    result = subprocess.run(sandbox_emitter.spool_hook_command("stop"), shell=True,
                            input="{}", text=True, capture_output=True, timeout=30,
                            env=env, cwd=tmp_path)
    assert result.returncode == 0
    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
def test_pi_extension_spool_mode_writes_events_without_a_hook(tmp_path):
    extension = tmp_path / "extensions" / "tongs-agents.js"
    extension.parent.mkdir()
    extension.write_bytes(sandbox_emitter.pi_extension_source())
    spool = tmp_path / "spool"
    runner = tmp_path / "runner.mjs"
    runner.write_text(
        f"import ext from {json.dumps(extension.as_uri())};\n"
        "const handlers = new Map();\n"
        "ext({ on: (name, handler) => handlers.set(name, handler) });\n"
        "const ctx = { cwd: '/sandbox/w', sessionManager: { getSessionId: () => 'pi-1' } };\n"
        "const fire = (name, event = {}) => handlers.get(name)?.({ type: name, ...event }, ctx);\n"
        "await fire('session_start', { reason: 'startup' });\n"
        "await fire('before_agent_start', {});\n"
        "await fire('session_shutdown', {});\n"
    )
    env = {**os.environ, "TONGS_AGENTS_SPOOL": str(spool),
           "PI_CODING_AGENT_DIR": str(tmp_path / "empty")}
    result = subprocess.run([NODE, str(runner)], env=env, capture_output=True, text=True,
                            timeout=30)
    assert result.returncode == 0, result.stderr
    names = sorted(path.name for path in spool.iterdir())
    assert len(names) == 3
    assert all(name.endswith("-fleet-event.json") and len(name.split("-")[0]) == 19
               for name in names)
    payloads = [json.loads((spool / name).read_text()) for name in names]
    assert [payload["status"] for payload in payloads] == ["started", "running", "ended"]
    assert [payload["sequence"] for payload in payloads] == [1, 2, 3]


def test_module_entry_point_is_importable():
    result = subprocess.run([sys.executable, "-c", "import tongs_agents.sandbox.__main__"],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
