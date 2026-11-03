"""The openshell CLI wrapper (rows 1 and 2): argv only, NO_COLOR, bounds, version floor."""

import sys

import pytest

from tests.test_sandbox_support import create_sandbox, fake, posix_only  # noqa: F401
from tongs_agents.sandbox import openshell
from tongs_agents.sandbox.openshell import OpenShell, OpenShellError


@pytest.mark.parametrize("text, expected", [
    ("openshell 0.1.6", (0, 1, 6)),
    ("openshell 0.1.6-rhaiv.4", (0, 1, 6)),
    ("openshell 0.1.7-pre.6\n", (0, 1, 7)),
    ("0.2.0", (0, 2, 0)),
    ("openshell dev", None),
    ("", None),
])
def test_version_parses_the_leading_triple(text, expected):
    assert openshell.parse_version(text) == expected


@pytest.mark.parametrize("version, error, warnings", [
    ((0, 0, 9), "older than 0.1.0", 0),
    ((0, 1, 0), None, 1),
    ((0, 1, 6), None, 0),
    ((0, 1, 9), None, 0),
    ((0, 2, 0), None, 1),
    (None, "cannot read", 0),
])
def test_version_floor(version, error, warnings):
    found, notes = openshell.version_problems(version)
    assert (found is None) == (error is None)
    if error:
        assert error in found
    assert len(notes) == warnings


def test_flavour_is_reported_from_the_suffix_only():
    assert openshell.flavour("openshell 0.1.6-rhaiv.4") == "midstream"
    assert openshell.flavour("openshell 0.1.6") == "upstream"


def test_missing_cli_is_a_clear_error(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(OpenShellError, match="not found on PATH"):
        OpenShell()


@posix_only
def test_calls_carry_no_color_and_the_gateway_flag(fake):
    client = OpenShell(gateway="local")
    assert client.get("absent") is None
    last = fake.raw_calls()[-1]
    assert (last["argv"], last["no_color"]) == (
        ["-g", "local", "sandbox", "get", "absent", "-o", "json"], "1"
    )


@posix_only
def test_too_old_cli_is_refused(fake, monkeypatch):
    monkeypatch.setenv("FAKE_OPENSHELL_VERSION", "0.0.9")
    with pytest.raises(OpenShellError, match="older than 0.1.0"):
        OpenShell().checked_version()
    monkeypatch.setenv("FAKE_OPENSHELL_VERSION", "0.1.6-rhaiv.4")
    assert OpenShell().checked_version() == ((0, 1, 6), [])


@posix_only
def test_get_parses_name_phase_and_labels(fake):
    create_sandbox(fake, "box", profile="p")
    info = OpenShell().get("box")
    assert (info.name, info.phase) == ("box", "Ready")
    assert info.label("tongs-agents.profile") == "p"


@posix_only
def test_get_failure_other_than_not_found_raises(fake, monkeypatch):
    monkeypatch.setenv("FAKE_OPENSHELL_FAIL", "get")
    with pytest.raises(OpenShellError, match="gateway unreachable"):
        OpenShell().get("box")


def test_unrecognized_json_is_not_a_sandbox():
    assert openshell.parse_sandbox("[]") is None
    assert openshell.parse_sandbox('{"name": "x"}') is None
    assert openshell.parse_sandbox("not json") is None


def test_exact_exec_and_connect_argv():
    client = OpenShell("/opt/bin/openshell", gateway=None, base_env={})
    argv = client.exec_tty_argv(
        "box", ["claude", "--model", "x"], workdir="/sandbox/workspace",
        env=[("TONGS_AGENTS_LAUNCH_ID", "a" * 32), ("CLAUDE_CONFIG_DIR", "/sandbox/c")],
    )
    assert argv == [
        "/opt/bin/openshell", "sandbox", "exec", "-n", "box", "--tty",
        "--workdir", "/sandbox/workspace",
        "--env", f"TONGS_AGENTS_LAUNCH_ID={'a' * 32}",
        "--env", "CLAUDE_CONFIG_DIR=/sandbox/c",
        "--", "claude", "--model", "x",
    ]
    assert client.connect_argv("box") == ["/opt/bin/openshell", "sandbox", "connect", "box"]


@pytest.mark.parametrize("name", ["ANTHROPIC_API_KEY", "GITHUB_TOKEN", "PATH", "HOME"])
def test_env_outside_the_allowlist_is_refused(name):
    client = OpenShell("/opt/bin/openshell", base_env={})
    with pytest.raises(OpenShellError, match=f"refusing to pass {name}"):
        client.exec_tty_argv("box", ["claude"], workdir="/sandbox/w", env=[(name, "x")])


def test_nul_in_an_argument_is_refused():
    client = OpenShell("/opt/bin/openshell", base_env={})
    with pytest.raises(OpenShellError, match="invalid openshell argument"):
        client.argv("sandbox", "get", "a\0b")


def test_policy_advisor_is_never_global(monkeypatch):
    seen = []
    client = OpenShell("/opt/bin/openshell", base_env={})
    monkeypatch.setattr(client, "check", lambda *args, **kwargs: seen.append(args))
    client.set_policy_advisor("box", True)
    assert seen == [("settings", "set", "box", "--key", "agent_policy_proposals_enabled",
                     "--value", "true")]
    assert "--global" not in seen[0]


def test_output_is_bounded():
    with pytest.raises(OpenShellError, match="printed more than"):
        openshell.run_bounded(
            [sys.executable, "-c", "import sys; sys.stdout.write('x' * 200000)"],
            env={}, timeout=30, max_bytes=1000,
        )


def test_timeout_kills_the_call():
    with pytest.raises(OpenShellError, match="timed out"):
        openshell.run_bounded([sys.executable, "-c", "import time; time.sleep(30)"],
                              env={}, timeout=0.5)


def test_input_is_written_to_stdin():
    result = openshell.run_bounded(
        [sys.executable, "-c", "import sys; print(sys.stdin.read().upper())"],
        env={}, timeout=30, input_bytes=b"spool",
    )
    assert (result.ok, result.stdout.strip()) == (True, "SPOOL")


@pytest.mark.skipif(sys.platform == "win32", reason="the fake CLI needs POSIX")
@pytest.mark.parametrize("existing", [False, True])
def test_fake_upload_nests_a_directory_like_upstream(fake, tmp_path, existing):
    create_sandbox(fake, "box")
    source = tmp_path / "proj"
    source.mkdir()
    (source / "a.txt").write_text("a")
    if existing:
        fake.fs("box", "/sandbox/dest").mkdir(parents=True)
    assert fake.run("sandbox", "upload", "box", str(source), "/sandbox/dest").returncode == 0
    assert fake.fs("box", "/sandbox/dest/proj/a.txt").read_text() == "a"
    assert not fake.fs("box", "/sandbox/dest/a.txt").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="the fake CLI needs POSIX")
def test_fake_upload_of_dot_lands_flat_and_resolves_against_cwd(fake, tmp_path):
    create_sandbox(fake, "box")
    source = tmp_path / "proj"
    source.mkdir()
    (source / "a.txt").write_text("a")
    result = OpenShell().run("sandbox", "upload", "box", ".", "/sandbox/dest", cwd=str(source))
    assert result.ok, result.stderr
    assert fake.fs("box", "/sandbox/dest/a.txt").read_text() == "a"
