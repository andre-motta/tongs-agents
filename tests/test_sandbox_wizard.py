"""Headless Textual journeys through the sandbox setup wizard (#38).

The wizard talks to the fake openshell CLI in tests/fixtures/fake_openshell
(instrumented evidence); the setup commands are unvalidated against a live
gateway until the maintainer's run (row 21).
"""

import asyncio
import threading
import json
from pathlib import Path

import pytest
from textual.widgets import Button, Checkbox, Input, Select, Static

from tests.test_sandbox_support import FAKE_DIR, fake, posix_only  # noqa: F401
from tongs_agents import paths
from tongs_agents.sandbox import setup
from tongs_agents.views.sandbox_wizard import SandboxWizardApp, SandboxWizardScreen

pytestmark = posix_only

SIZE = (110, 60)
SYSTEM_PATH = "/usr/bin:/bin"


@pytest.fixture(autouse=True)
def system_path(monkeypatch):
    """The fake runs probes on the host: keep a developer's own harness binaries off PATH."""
    monkeypatch.setenv("PATH", f"{FAKE_DIR}:{SYSTEM_PATH}")


def config_file() -> Path:
    return paths.config_dir() / "config.toml"


def created(fake) -> list[list[str]]:  # noqa: F811
    return [call for call in fake.calls() if call[:2] == ["sandbox", "create"]]


def text_of(screen: SandboxWizardScreen, selector: str) -> str:
    return str(screen.query_one(selector, Static).render())


async def settle(app, pilot) -> None:
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()
    await pilot.pause()


async def press(app, pilot, selector: str) -> None:
    app.wizard.query_one(selector, Button).press()
    await settle(app, pilot)


async def next_step(app, pilot) -> None:
    await press(app, pilot, "#next")


def plan_for(folder: Path) -> setup.Plan:
    return setup.default_plan("claude", folder)


def test_full_journey_reviews_the_policy_before_the_probe_sandbox(fake, tmp_path):  # noqa: F811
    project = tmp_path / "project"
    project.mkdir()
    state = fake.state()
    state["providers"] = {"codex": "codex"}
    (fake.home / "state.json").write_text(json.dumps(state))

    async def journey():
        app = SandboxWizardApp(plan_for(project))
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            screen = app.wizard
            assert screen.step == "step-host"
            host = text_of(screen, "#host-checks")
            assert "Gateway default is reachable" in host
            assert "OPENSHELL_TELEMETRY_ENABLED=false" in host
            assert "Providers on the gateway: codex" in host
            await next_step(app, pilot)

            assert screen.step == "step-plan"
            screen.query_one("#harness", Select).value = "codex"
            await settle(app, pilot)
            assert screen.query_one("#profile", Input).value == "codex-boxed"
            assert screen.query_one("#preset-openai", Checkbox).value
            assert not screen.query_one("#preset-anthropic", Checkbox).value
            screen.query_one("#image", Input).value = "example/agent:1"
            await next_step(app, pilot)

            assert screen.step == "step-network"
            screen.query_one("#preset-github", Checkbox).value = True
            await settle(app, pilot)
            assert screen.query_one("#providers", Input).value == "codex, github"
            assert created(fake) == []
            await next_step(app, pilot)

            assert screen.step == "step-review"
            policy = text_of(screen, "#policy")
            assert "api.openai.com" in policy and "api.github.com" in policy
            assert "anthropic" not in policy
            assert created(fake) == []
            assert not screen.query_one("#probe", Button).disabled
            await press(app, pilot, "#probe")
            status = text_of(screen, "#review-status")
            assert "codex: MISSING" in status
            [create] = created(fake)
            assert "--from" in create and "example/agent:1" in create
            assert not any(arg.startswith("--provider") or arg == "--upload" for arg in create)
            assert fake.state()["sandboxes"] == {}
            await press(app, pilot, "#save")
            saved = paths.config_dir() / "policies" / "codex-boxed.yaml"
            assert saved.read_text() == policy
            await next_step(app, pilot)

            assert screen.step == "step-finish"
            final = text_of(screen, "#finish")
            assert "openshell provider create --name github --type github" in final
            assert "--name codex" not in final
            assert "tongs-agents workspace --profile codex-boxed" in final
            assert "[profiles.codex-boxed.sandbox]" in final
            await next_step(app, pilot)
        return app.return_value

    result = asyncio.run(journey())
    assert result is not None and "[profiles.codex-boxed]" in result
    assert not config_file().exists()


def test_unreachable_gateway_disables_the_probe(fake, monkeypatch, tmp_path):  # noqa: F811
    monkeypatch.setenv("FAKE_OPENSHELL_FAIL", "status")

    async def journey():
        app = SandboxWizardApp(plan_for(tmp_path))
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            screen = app.wizard
            host = text_of(screen, "#host-checks")
            assert "not reachable" in host
            assert "openshell status" in host
            for _ in range(3):
                await next_step(app, pilot)
            assert screen.step == "step-review"
            assert screen.query_one("#probe", Button).disabled
            await next_step(app, pilot)
            assert "openshell status" in text_of(screen, "#finish")

    asyncio.run(journey())
    assert created(fake) == []


def test_missing_cli_is_reported_with_install_hint(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))

    async def journey():
        app = SandboxWizardApp(plan_for(tmp_path))
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            host = text_of(app.wizard, "#host-checks")
            assert "openshell CLI not found" in host
            assert "never installs" in host

    asyncio.run(journey())


def test_invalid_answers_stay_on_the_step(fake, tmp_path):  # noqa: F811
    async def journey():
        app = SandboxWizardApp(plan_for(tmp_path))
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            screen = app.wizard
            await next_step(app, pilot)
            screen.query_one("#folder", Input).value = str(tmp_path / "absent")
            await next_step(app, pilot)
            assert screen.step == "step-plan"
            assert "not a directory" in text_of(screen, "#wizard-error")
            screen.query_one("#folder", Input).value = str(tmp_path)
            screen.query_one("#profile", Input).value = "Bad Name"
            await next_step(app, pilot)
            assert screen.step == "step-plan"
            assert "profile name" in text_of(screen, "#wizard-error")

    asyncio.run(journey())


def test_a_policy_changed_after_review_cannot_be_probed(fake, tmp_path):  # noqa: F811
    async def journey():
        app = SandboxWizardApp(plan_for(tmp_path))
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            screen = app.wizard
            for _ in range(3):
                await next_step(app, pilot)
            assert screen.step == "step-review"
            screen.query_one("#preset-npm", Checkbox).value = True
            await press(app, pilot, "#probe")
            assert "review it" in text_of(screen, "#wizard-error")
            await next_step(app, pilot)
            assert screen.step == "step-review"

    asyncio.run(journey())
    assert created(fake) == []


def test_saving_never_overwrites_a_policy_file(fake, tmp_path):  # noqa: F811
    existing = tmp_path / "policy.yaml"
    existing.write_text("mine\n")

    async def journey():
        plan = setup.replace(plan_for(tmp_path), policy_path=existing)
        app = SandboxWizardApp(plan)
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            screen = app.wizard
            for _ in range(3):
                await next_step(app, pilot)
            screen.query_one("#policy-path", Input).value = str(existing)
            await press(app, pilot, "#save")
            assert "refusing to overwrite" in text_of(screen, "#wizard-error")

    asyncio.run(journey())
    assert existing.read_text() == "mine\n"


@pytest.mark.parametrize("key", ["escape", "ctrl+q"])
def test_cancel_returns_nothing_and_writes_nothing(fake, tmp_path, key):  # noqa: F811
    async def journey():
        app = SandboxWizardApp(plan_for(tmp_path))
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            await pilot.press(key)
            await pilot.pause()
        return app.return_value

    assert asyncio.run(journey()) is None
    assert not config_file().exists()
    assert not (paths.config_dir() / "policies").exists()
    assert created(fake) == []


def test_a_gateway_changed_after_the_host_check_blocks_the_probe(fake, tmp_path):  # noqa: F811
    async def journey():
        app = SandboxWizardApp(plan_for(tmp_path))
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            screen = app.wizard
            screen.query_one("#gateway", Input).value = "other"
            for _ in range(3):
                await next_step(app, pilot)
            assert screen.step == "step-review"
            assert screen.query_one("#probe", Button).disabled
            assert "Check again" in text_of(screen, "#review-status")
            await press(app, pilot, "#back")
            await press(app, pilot, "#back")
            await press(app, pilot, "#back")
            await press(app, pilot, "#recheck")
            for _ in range(3):
                await next_step(app, pilot)
            assert not screen.query_one("#probe", Button).disabled

    asyncio.run(journey())
    assert ["-g", "other", "status", "-o", "json"] in fake.calls()


@pytest.mark.parametrize("gateway", ["-g", "--evil", "bad name", "x" * 65])
def test_an_invalid_gateway_is_a_field_error_and_runs_nothing(fake, tmp_path,  # noqa: F811
                                                              gateway):
    async def journey():
        app = SandboxWizardApp(setup.replace(plan_for(tmp_path), gateway=gateway))
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            screen = app.wizard
            assert "A gateway name" in text_of(screen, "#gateway-error")
            assert "Fix the gateway name" in text_of(screen, "#host-checks")
            assert fake.calls() == [] and screen.report is None
            screen.query_one("#gateway", Input).value = "local"
            await press(app, pilot, "#recheck")
            assert text_of(screen, "#gateway-error") == ""
            assert ["-g", "local", "status", "-o", "json"] in fake.calls()
            screen.query_one("#gateway", Input).value = gateway
            calls = len(fake.calls())
            await press(app, pilot, "#recheck")
            assert "A gateway name" in text_of(screen, "#gateway-error")
            assert len(fake.calls()) == calls

    asyncio.run(journey())


def test_cancel_waits_for_a_running_probe_and_names_its_sandbox(fake, tmp_path):  # noqa: F811
    gate = threading.Event()
    seen = []

    def slow_probe(plan, reviewed, *, name):
        seen.append(name)
        gate.wait(10)
        return setup.ProbeResult(name, {}, (), deleted=True, gateway=plan.gateway)

    async def journey():
        app = SandboxWizardApp(setup.replace(plan_for(tmp_path), gateway="local"),
                               probe=slow_probe)
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            screen = app.wizard
            for _ in range(3):
                await next_step(app, pilot)
            assert screen.step == "step-review"
            screen.query_one("#probe", Button).press()
            for _ in range(100):
                if seen:
                    break
                await pilot.pause(0.01)
            [name] = seen
            assert name.startswith(setup.PROBE_PREFIX)
            assert name in text_of(screen, "#review-status")
            await pilot.press("ctrl+q")
            await pilot.pause()
            assert app.is_running and app.screen is screen
            error = text_of(screen, "#wizard-error")
            assert name in error
            assert f"tongs-agents sandbox delete --gateway local {name}" in error
            screen.action_cancel()
            await pilot.pause()
            assert app.is_running
            gate.set()
            await settle(app, pilot)
            assert screen.probing is None and "Image looks ready" in text_of(
                screen, "#review-status")
            await pilot.press("ctrl+q")
            await pilot.pause()
        return app.return_value

    assert asyncio.run(journey()) is None
    assert not config_file().exists()
