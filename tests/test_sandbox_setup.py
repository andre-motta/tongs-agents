"""``tongs-agents sandbox setup`` (#38): host checks, policy review, image probe, snippet.

Every OpenShell call runs against the fake CLI in tests/fixtures/fake_openshell
(instrumented evidence); ``provider list -o json`` and the probe are
unvalidated against a live gateway until the maintainer's run (row 21).
"""

import json
import os
import stat
import tomllib
from pathlib import Path

import pytest

from tests.test_sandbox_support import FAKE_DIR, fake, posix_only  # noqa: F401
from tongs_agents import cli, paths
from tongs_agents.commands import sandbox as sandbox_command
from tongs_agents.profiles import parse_profile
from tongs_agents.sandbox import setup
from tongs_agents.sandbox.openshell import LAUNCH_LABEL

pytestmark = posix_only

SYSTEM_PATH = "/usr/bin:/bin"


@pytest.fixture(autouse=True)
def system_path(monkeypatch):
    """The fake runs probes on the host: keep a developer's own harness binaries off PATH."""
    monkeypatch.setenv("PATH", f"{FAKE_DIR}:{SYSTEM_PATH}")


def run(*argv: str) -> int:
    parser, handlers = cli.build_parser()
    args = parser.parse_args(["sandbox", "setup", "--print", *argv])
    return handlers[args.command](args) or 0


def add_providers(fake, providers):  # noqa: F811
    state = fake.state()
    state["providers"] = providers
    (fake.home / "state.json").write_text(json.dumps(state))


def calls_by_verb(fake) -> list[list[str]]:  # noqa: F811
    return [call[:2] for call in fake.calls()]


def plan(tmp_path, **changes) -> setup.Plan:
    return setup.replace(setup.default_plan("claude", tmp_path), **changes)


def bin_dir(tmp_path, *names) -> Path:
    directory = tmp_path / "bin"
    directory.mkdir(exist_ok=True)
    for name in names:
        tool = directory / name
        tool.write_text("#!/bin/sh\nexit 0\n")
        tool.chmod(0o755)
    return directory


# Host journeys


def test_missing_cli_prints_install_hint_and_fails(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("PATH", str(tmp_path))
    assert run("--folder", str(tmp_path)) == 1
    out = capsys.readouterr().out
    assert "openshell CLI not found" in out
    assert "github.com/NVIDIA/OpenShell/releases" in out
    assert "never installs" in out
    assert "[profiles.claude-boxed]" in out


def test_old_version_is_refused_without_asking_the_gateway(  # noqa: F811
        fake, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("FAKE_OPENSHELL_VERSION", "0.0.9")
    assert run("--folder", str(tmp_path)) == 1
    out = capsys.readouterr().out
    assert "older than 0.1.0" in out
    assert fake.calls() == [["--version"]]


def test_below_floor_version_warns_but_is_ready(fake, monkeypatch, tmp_path, capsys):  # noqa: F811
    monkeypatch.setenv("FAKE_OPENSHELL_VERSION", "0.1.2")
    assert run("--folder", str(tmp_path)) == 0
    assert "older than the supported 0.1.6" in capsys.readouterr().out


def test_unreachable_gateway_prints_commands_and_fails(fake, monkeypatch, tmp_path,  # noqa: F811
                                                       capsys):
    monkeypatch.setenv("FAKE_OPENSHELL_FAIL", "status")
    assert run("-g", "local", "--folder", str(tmp_path), "--probe", "--yes") == 1
    out = capsys.readouterr().out
    assert "Gateway local is not reachable" in out
    assert "openshell -g local status" in out
    assert "Not probing the image" in out
    assert calls_by_verb(fake) == [["--version"], ["-g", "local"]]


def test_ready_host_reports_flavour_providers_and_telemetry(fake, monkeypatch,  # noqa: F811
                                                            tmp_path):
    monkeypatch.setenv("FAKE_OPENSHELL_VERSION", "0.1.6-rhaiv.4")
    add_providers(fake, {"claude-code": "claude-code", "github": "github"})
    report = setup.inspect_host(landlock=lambda: 6, kvm=lambda: False)
    assert report.ready and report.flavour == "midstream"
    assert report.providers == {"claude-code": "claude-code", "github": "github"}
    names = {check.name: check for check in report.checks}
    assert names["openshell-gateway"].level == "ok"
    assert "OPENSHELL_TELEMETRY_ENABLED=false" in names["openshell-telemetry"].remediation
    assert "openshell-runtime" in names
    assert setup.host_commands(report) == []
    assert fake.calls() == [["--version"], ["status", "-o", "json"],
                            ["provider", "list", "-o", "json"]]


def test_provider_list_failure_is_a_note_not_an_error(fake, monkeypatch, tmp_path):  # noqa: F811
    monkeypatch.setenv("FAKE_OPENSHELL_FAIL", "provider")
    report = setup.inspect_host()
    assert report.ready and report.providers is None
    assert "provider list failed" in report.provider_error
    reviewed = setup.review(plan(tmp_path))
    final = setup.finish(plan(tmp_path), report, reviewed)
    assert any("Could not list providers" in note for note in final.notes)
    assert "--name claude-code" in final.text()


@pytest.mark.parametrize("text", ["", "{}", "not json", '[{"name": "../x", "type": 1}]'])
def test_parse_providers_is_strict(text):
    parsed = setup.parse_providers(text)
    assert parsed in (None, {})


def test_runtime_hint_names_drivers():
    check = setup.runtime_check(lambda name: "/usr/bin/podman" if name == "podman" else None,
                                lambda: True)
    assert check.message.endswith("podman, KVM")
    assert "No Podman" in setup.runtime_check(lambda name: None, lambda: False).message


# Plan, policy and snippet


def test_policy_is_valid_yaml_of_the_documented_shape(tmp_path):
    yaml = pytest.importorskip("yaml")
    full = plan(tmp_path, presets=tuple(setup.PRESETS))
    document = yaml.safe_load(setup.policy_text(full))
    expected = json.loads(json.dumps(setup.policy_document(full)))
    assert document == expected
    empty = yaml.safe_load(setup.policy_text(plan(tmp_path, presets=())))
    assert empty["network_policies"] == {}


def test_policy_shape_without_yaml(tmp_path):
    text = setup.policy_text(plan(tmp_path, presets=("anthropic", "pypi")))
    assert text.splitlines()[3] == "version: 1"
    assert "  tongs_anthropic:" in text and "  tongs_pypi:" in text
    assert '      - host: "api.anthropic.com"' in text
    assert '      - path: "/usr/bin/claude"' in text
    assert "approval" not in text and "auto" not in text
    assert setup.policy_text(plan(tmp_path, presets=())).rstrip().endswith("network_policies: {}")


def test_harness_suggestions():
    assert setup.default_plan("codex", Path("/")).presets == ("openai",)
    assert setup.default_plan("pi", Path("/")).providers == ("openrouter",)
    assert setup.suggested_providers("claude", ("github", "npm")) == ("claude-code", "github")


@pytest.mark.parametrize("changes, message", [
    ({"profile": "Bad"}, "profile name"),
    ({"harness": "other"}, "harness"),
    ({"presets": ("bogus",)}, "network preset"),
    ({"providers": ("bad name",)}, "provider"),
    ({"image": "bad image"}, "image"),
    ({"gateway": "-g"}, "gateway"),
    ({"policy_path": Path("relative.yaml")}, "absolute"),
    ({"policy_path": Path("/tmp/policy.txt")}, ".yaml"),
])
def test_invalid_plans_are_refused(tmp_path, changes, message):
    with pytest.raises(setup.SetupError, match=message):
        plan(tmp_path, **changes).check()


def test_missing_folder_is_refused(tmp_path):
    with pytest.raises(setup.SetupError, match="not a directory"):
        plan(tmp_path, folder=tmp_path / "absent").check()


def test_snippet_loads_as_a_sandboxed_profile(tmp_path):
    odd = tmp_path / 'my "project"'
    odd.mkdir()
    chosen = plan(odd, image="example/agent:1", gateway="local",
                  providers=("claude-code", "github"))
    snippet = setup.profile_snippet(chosen)
    profile = parse_profile("claude-boxed", tomllib.loads(snippet)["profiles"]["claude-boxed"])
    assert profile.cwd == odd and profile.harness == "claude"
    assert profile.sandbox.providers == ("claude-code", "github")
    assert profile.sandbox.image == "example/agent:1"
    assert profile.sandbox.gateway == "local"
    assert profile.sandbox.policy == paths.config_dir() / "policies" / "claude-boxed.yaml"
    assert "env" not in snippet


def test_finish_needs_the_reviewed_policy(tmp_path):
    chosen = plan(tmp_path)
    with pytest.raises(setup.SetupError, match="review"):
        setup.finish(chosen, None, None)
    reviewed = setup.review(chosen)
    with pytest.raises(setup.SetupError, match="review"):
        setup.finish(plan(tmp_path, presets=("npm",)), None, reviewed)
    final = setup.finish(plan(tmp_path, image="example/agent:1"), None, reviewed)
    assert "tongs-agents workspace --profile claude-boxed" in final.text()


def test_existing_providers_need_no_command(tmp_path):
    report = setup.HostReport(checks=(), executable="/x", gateway_ok=True,
                              providers={"claude-code": "claude-code"})
    chosen = plan(tmp_path, providers=("claude-code", "mine"))
    needs = {need.name: need for need in setup.provider_needs(chosen, report)}
    assert needs["claude-code"].command() is None
    assert needs["mine"].command() == (
        "openshell provider create --name mine --type <type> --from-existing"
    )


# Provider types upstream's ``normalize_provider_type`` and
# ``normalize_inference_provider_type`` accept (crates/openshell-providers/src/lib.rs,
# crates/openshell-core/src/inference.rs), read from source, not run.
UPSTREAM_PROVIDER_TYPES = {
    "claude-code", "codex", "copilot", "opencode", "google-cloud", "generic", "gitlab",
    "github", "outlook", "openai", "anthropic", "nvidia", "deepinfra", "aws-bedrock",
    "google-vertex-ai",
}
# Types whose --from-existing discovery reads an environment variable upstream
# (providers/<type>.rs ProviderDiscoverySpec); generic discovers nothing.
DISCOVERABLE_TYPES = {"claude-code", "codex", "github"}


def test_every_preset_provider_has_an_upstream_type():
    for key, preset in setup.PRESETS.items():
        if preset.provider is None:
            assert preset.provider_type is None and preset.credential is None, key
            continue
        assert preset.provider_type in UPSTREAM_PROVIDER_TYPES, key
        if preset.provider_type not in DISCOVERABLE_TYPES:
            assert preset.credential, key


@pytest.mark.parametrize("harness, presets", [
    ("claude", ("anthropic", "github")),
    ("codex", ("openai", "pypi")),
    ("pi", ("openrouter", "npm")),
])
def test_printed_provider_commands_match_upstream_cli(tmp_path, harness, presets):
    chosen = plan(tmp_path, harness=harness, presets=presets,
                  providers=setup.suggested_providers(harness, presets))
    commands = [need.command() for need in setup.provider_needs(chosen)]
    assert commands
    for command in commands:
        words = command.split()
        assert words[:3] == ["openshell", "provider", "create"]
        options, rest = {}, words[3:]
        while rest:
            flag = rest.pop(0)
            options[flag] = True if flag == "--from-existing" else rest.pop(0)
        # Upstream ProviderCommands::Create: --name, --type and exactly one
        # credential source (--from-existing or --credential KEY).
        assert set(options) <= {"--name", "--type", "--credential", "--from-existing"}
        assert options["--type"] in UPSTREAM_PROVIDER_TYPES
        assert ("--from-existing" in options) != ("--credential" in options)
        if options["--type"] not in DISCOVERABLE_TYPES:
            assert "--from-existing" not in options
        if "--credential" in options:
            assert "=" not in options["--credential"]


def test_openrouter_is_a_generic_provider_with_pis_key(tmp_path):
    chosen = plan(tmp_path, harness="pi", presets=("openrouter",), providers=("openrouter",))
    [need] = setup.provider_needs(chosen)
    assert need.command() == (
        "openshell provider create --name openrouter --type generic "
        "--credential OPENROUTER_API_KEY"
    )
    report = setup.HostReport(checks=(), executable="/x", gateway_ok=True,
                              providers={"openrouter": "openai"})
    [need] = setup.provider_needs(chosen, report)
    assert need.command() is None


# Writing the policy


def test_write_policy_is_private_and_never_overwrites(tmp_path):
    reviewed = setup.review(plan(tmp_path))
    target = tmp_path / "deep" / "policy.yaml"
    assert setup.write_policy(reviewed, target) == target
    assert target.read_text() == reviewed.text
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    target.write_text("edited\n")
    with pytest.raises(setup.SetupError, match="refusing to overwrite"):
        setup.write_policy(reviewed, target)
    assert target.read_text() == "edited\n"
    link = tmp_path / "link.yaml"
    link.symlink_to(tmp_path / "elsewhere.yaml")
    with pytest.raises(setup.SetupError):
        setup.write_policy(reviewed, link)
    assert not (tmp_path / "elsewhere.yaml").exists()


def test_cli_writes_only_the_policy_file(fake, tmp_path, capsys):  # noqa: F811
    policy = tmp_path / "out" / "boxed.yaml"
    assert run("--folder", str(tmp_path), "--profile", "boxed", "--policy-file", str(policy),
               "--write-policy") == 0
    out = capsys.readouterr().out
    assert policy.exists() and f"Saved the policy as {policy}" in out
    assert not (paths.config_dir() / "config.toml").exists()
    assert "Save the reviewed policy" not in out
    assert run("--folder", str(tmp_path), "--policy-file", str(policy), "--write-policy") == 1
    assert "refusing to overwrite" in capsys.readouterr().err


# Probe


def test_probe_refuses_an_unreviewed_policy(fake, tmp_path):  # noqa: F811
    with pytest.raises(setup.SetupError, match="review"):
        setup.probe_image(plan(tmp_path), None)
    reviewed = setup.review(plan(tmp_path))
    with pytest.raises(setup.SetupError, match="review"):
        setup.probe_image(plan(tmp_path, presets=("github",)), reviewed)
    assert fake.calls() == []


def test_probe_uses_the_reviewed_policy_and_deletes_its_sandbox(fake, monkeypatch,  # noqa: F811
                                                                tmp_path):
    tools = bin_dir(tmp_path, "claude", "tongs-agents")
    monkeypatch.setenv("PATH", f"{FAKE_DIR}:{tools}:{SYSTEM_PATH}")
    chosen = plan(tmp_path, image="example/agent:1", chat=True)
    reviewed = setup.review(chosen)
    result = setup.probe_image(chosen, reviewed)
    assert result.ok and result.deleted
    assert result.found["claude"] == str(tools / "claude")
    assert result.found["tongs-agents"] == str(tools / "tongs-agents")
    create = next(call for call in fake.calls() if call[:2] == ["sandbox", "create"])
    assert create[2:4] == ["--name", result.sandbox]
    assert result.sandbox.startswith("tongs-setup-")
    labels = [create[index + 1] for index, arg in enumerate(create) if arg == "--label"]
    assert len(labels) == 1 and labels[0].startswith(f"{LAUNCH_LABEL}=")
    assert "--provider" not in create and "--upload" not in create
    assert not any(arg.startswith("--approval") for arg in create)
    assert ["sandbox", "delete", result.sandbox] in fake.calls()
    assert fake.state()["sandboxes"] == {}
    assert not Path(create[create.index("--policy") + 1]).exists()


def test_probe_reports_missing_tools(fake, tmp_path):  # noqa: F811
    chosen = plan(tmp_path, harness="codex", profile="c", presets=("openai",))
    result = setup.probe_image(chosen, setup.review(chosen))
    assert not result.ok
    assert "codex" in result.missing
    assert "codex: MISSING" in result.lines()


def test_probe_deletes_its_sandbox_when_exec_fails(fake, monkeypatch, tmp_path):  # noqa: F811
    monkeypatch.setenv("FAKE_OPENSHELL_FAIL", "exec")
    chosen = plan(tmp_path)
    result = setup.probe_image(chosen, setup.review(chosen))
    assert "exec failed" in result.error and result.deleted
    assert fake.state()["sandboxes"] == {}


def test_probe_reports_a_sandbox_it_could_not_delete(fake, monkeypatch, tmp_path):  # noqa: F811
    monkeypatch.setenv("FAKE_OPENSHELL_FAIL", "delete")
    chosen = plan(tmp_path)
    result = setup.probe_image(chosen, setup.review(chosen))
    assert not result.deleted and result.gateway is None
    assert f"run: tongs-agents sandbox delete {result.sandbox}" in result.lines()[-1]


def test_a_probe_left_on_a_named_gateway_names_it_in_the_hint(fake, monkeypatch,  # noqa: F811
                                                              tmp_path):
    monkeypatch.setenv("FAKE_OPENSHELL_FAIL", "delete")
    chosen = plan(tmp_path, gateway="local")
    result = setup.probe_image(chosen, setup.review(chosen))
    assert not result.deleted and result.gateway == "local"
    hint = result.lines()[-1].split("run: ", 1)[1]
    assert hint == f"tongs-agents sandbox delete --gateway local {result.sandbox}"
    assert all(call[:2] == ["-g", "local"] for call in fake.calls() if call != ["--version"])
    parser, _handlers = cli.build_parser()
    args = parser.parse_args(hint.split()[1:])
    assert (args.sandbox_action, args.gateway, args.name) == ("delete", "local",
                                                              [result.sandbox])


def test_probe_uses_the_name_it_is_given(fake, tmp_path):  # noqa: F811
    chosen = plan(tmp_path)
    name = setup.probe_name()
    assert setup.probe_image(chosen, setup.review(chosen), name=name).sandbox == name
    with pytest.raises(setup.SetupError, match="probe sandbox name"):
        setup.probe_image(chosen, setup.review(chosen), name="tongs-boxed")


def test_probe_stops_on_a_sandbox_that_never_gets_ready(fake, monkeypatch, tmp_path):  # noqa: F811
    monkeypatch.setenv("FAKE_OPENSHELL_PHASES", "Error")
    chosen = plan(tmp_path)
    result = setup.probe_image(chosen, setup.review(chosen))
    assert "phase Error" in result.error
    assert not [call for call in fake.calls() if call[:2] == ["sandbox", "exec"]]
    assert fake.state()["sandboxes"] == {}


def test_probe_create_failure_is_reported(fake, monkeypatch, tmp_path):  # noqa: F811
    monkeypatch.setenv("FAKE_OPENSHELL_FAIL", "create")
    chosen = plan(tmp_path)
    result = setup.probe_image(chosen, setup.review(chosen))
    assert "create failed" in result.error and result.deleted


def test_parse_probe_treats_output_as_untrusted():
    text = ("found sh /bin/sh\nfound sh /evil\nfound rm /bin/rm\n"
            "found tar \x1b[31m/usr/bin/tar\n" + "found bash /" + "a" * 500 + "\n")
    found, missing = setup.parse_probe(text, ("sh", "tar", "bash", "claude"))
    assert found["sh"] == "/bin/sh" and "rm" not in found
    assert found["tar"] == "/usr/bin/tar"
    assert len(found["bash"]) == setup.MAX_PROBE_PATH
    assert missing == ("claude",)


def test_cli_probe_needs_a_yes_off_a_terminal(fake, tmp_path, capsys):  # noqa: F811
    assert run("--folder", str(tmp_path), "--probe") == 1
    assert "pass --yes" in capsys.readouterr().out
    assert not [call for call in fake.calls() if call[:2] == ["sandbox", "create"]]


def test_cli_prints_the_policy_before_probing(fake, monkeypatch, tmp_path, capsys):  # noqa: F811
    tools = bin_dir(tmp_path, "claude")
    monkeypatch.setenv("PATH", f"{FAKE_DIR}:{tools}:{SYSTEM_PATH}")
    assert run("--folder", str(tmp_path), "--network", "github", "--probe", "--yes") == 0
    out = capsys.readouterr().out
    assert out.index("Generated policy") < out.index("Image probe")
    assert "openshell provider create --name github --type github --from-existing" in out
    assert 'providers = ["claude-code", "github"]' in out


def test_network_none_means_no_entries(fake, tmp_path, capsys):  # noqa: F811
    assert run("--folder", str(tmp_path), "--network", "none") == 0
    out = capsys.readouterr().out
    assert "network_policies: {}" in out
    assert "allows no network access" in out


def test_setup_without_a_terminal_prints(fake, monkeypatch, tmp_path, capsys):  # noqa: F811
    monkeypatch.chdir(tmp_path)
    parser, handlers = cli.build_parser()
    args = parser.parse_args(["sandbox", "setup"])
    assert handlers[args.command](args) == 0
    assert "Generated policy" in capsys.readouterr().out


def test_doctor_lines_still_come_from_the_shared_checks(fake, monkeypatch):  # noqa: F811
    monkeypatch.setattr(sandbox_command, "landlock_abi", lambda: 2)
    checks = {check.name: check for check in sandbox_command.doctor_checks()}
    assert checks["openshell"].level == "ok"
    if os.uname().sysname == "Linux":
        assert checks["landlock"].level == "warn"


def test_setup_never_calls_global_or_approval_commands(fake, monkeypatch, tmp_path):  # noqa: F811
    monkeypatch.setenv("PATH", f"{FAKE_DIR}:{bin_dir(tmp_path, 'claude')}:{SYSTEM_PATH}")
    assert run("--folder", str(tmp_path), "--probe", "--yes") == 0
    for call in fake.calls():
        assert "--global" not in call
        assert call[0] not in ("settings", "rule", "policy")


def test_print_refuses_an_invalid_gateway_before_any_cli_call(fake, tmp_path,  # noqa: F811
                                                              capsys):
    assert run("-g", "bad name", "--folder", str(tmp_path)) == 1
    assert "gateway" in capsys.readouterr().err
    assert fake.calls() == []
