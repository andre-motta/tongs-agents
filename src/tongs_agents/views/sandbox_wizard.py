"""The sandbox setup wizard as a Textual flow (#38; logic in ``sandbox.setup``).

Steps: host checks, folder and harness, network presets and providers,
policy review (save the policy, probe the image), then the commands and the
profile snippet. The probe is the only step that starts a sandbox, and it is
offered on the review step only, so the generated policy is on screen first.
While it runs the wizard cannot be cancelled or finished (the probe deletes
its sandbox when it ends), and the status line names the sandbox and the
command that deletes it.
The wizard never writes the tongs-agents config file and never installs,
starts or configures OpenShell (row 18). ``SandboxWizardScreen`` dismisses
with the final text, or None when cancelled.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import (
    Button,
    Checkbox,
    ContentSwitcher,
    Footer,
    Header,
    Input,
    Label,
    Select,
    Static,
)

from tongs_agents.commands.doctor import Check
from tongs_agents.profiles import SANDBOX_TOKEN_RE
from tongs_agents.sandbox import setup
from tongs_agents.sandbox.setup import HostReport, Plan, ReviewedPolicy, SetupError

STEPS = ("step-host", "step-plan", "step-network", "step-review", "step-finish")
STEP_TITLES = {
    "step-host": "1/5 OpenShell on this host",
    "step-plan": "2/5 Folder and harness",
    "step-network": "3/5 Network and credentials",
    "step-review": "4/5 Review the policy",
    "step-finish": "5/5 Commands and profile",
}
LEVEL_STYLES = {"ok": "green", "info": "cyan", "warn": "yellow", "error": "bold red"}
SPLIT_RE = re.compile(r"[\s,]+")


def checks_text(report: HostReport) -> Text:
    text = Text()
    for check in report.checks:
        text.append(f"{check.level.upper():5} ", style=LEVEL_STYLES.get(check.level))
        text.append(f"{check.name}: {check.message}\n")
        if check.remediation and check.level != "ok":
            text.append(f"      {check.remediation}\n", style="dim")
    if report.providers is not None:
        names = ", ".join(sorted(report.providers)) or "none"
        text.append(f"\nProviders on the gateway: {names}\n")
    steps = setup.host_commands(report)
    if steps:
        text.append("\nRun these yourself first:\n", style="bold")
        for line in steps:
            text.append(f"  {line}\n")
    else:
        text.append("\nThe host is ready for sandboxes.\n", style="green")
    return text


class SandboxWizardScreen(Screen[str | None]):
    """Walks through ``sandbox.setup``; dismisses with the final text."""

    DEFAULT_CSS = """
    SandboxWizardScreen #wizard-title {
        text-style: bold;
        padding: 0 1;
    }
    SandboxWizardScreen ContentSwitcher {
        height: 1fr;
    }
    SandboxWizardScreen ContentSwitcher > * {
        padding: 0 1;
    }
    SandboxWizardScreen .text {
        height: auto;
    }
    SandboxWizardScreen #wizard-error, SandboxWizardScreen #gateway-error {
        color: $error;
        height: auto;
        padding: 0 1;
    }
    SandboxWizardScreen #wizard-buttons {
        height: auto;
        padding: 0 1;
    }
    SandboxWizardScreen #review-buttons {
        height: auto;
    }
    SandboxWizardScreen Input, SandboxWizardScreen Select {
        width: 100%;
    }
    """

    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("ctrl+q", "cancel", "Cancel"),
    ]

    def __init__(self, plan: Plan | None = None, *,
                 inspect: Callable[..., HostReport] = setup.inspect_host,
                 probe: Callable[..., setup.ProbeResult] = setup.probe_image) -> None:
        super().__init__()
        self.start = plan or setup.default_plan()
        self._inspect = inspect
        self._probe = probe
        self.report: HostReport | None = None
        self.reviewed: ReviewedPolicy | None = None
        self.policy_written = False
        self.result: str | None = None
        self.probing: tuple[str, str | None] | None = None
        self._harness = self.start.harness

    # Layout

    def compose(self) -> ComposeResult:
        plan = self.start
        yield Header()
        yield Label(STEP_TITLES[STEPS[0]], id="wizard-title")
        with ContentSwitcher(initial=STEPS[0]):
            with VerticalScroll(id="step-host"):
                yield Label("Gateway (empty: openshell's default)")
                yield Input(plan.gateway or "", id="gateway", placeholder="default")
                yield Static("", id="gateway-error")
                yield Button("Check again", id="recheck")
                yield Static("Checking the host...", id="host-checks", classes="text")
            with VerticalScroll(id="step-plan"):
                yield Label("Project folder (uploaded into the sandbox)")
                yield Input("" if plan.folder is None else str(plan.folder), id="folder",
                            placeholder="a project folder, not your home folder or /")
                yield Label("Harness")
                yield Select([(name, name) for name in setup.SETUP_HARNESSES],
                             value=plan.harness, allow_blank=False, id="harness")
                yield Label("Profile name")
                yield Input(plan.profile, id="profile")
                yield Label("Image (empty: the gateway's default)")
                yield Input(plan.image or "", id="image", placeholder="registry/org/agent:tag")
            with VerticalScroll(id="step-network"):
                yield Label("Network access the policy allows (everything else is denied):")
                for key, preset in setup.PRESETS.items():
                    yield Checkbox(f"{key}: {preset.title}", key in plan.presets,
                                   id=f"preset-{key}")
                yield Label("OpenShell providers for credentials (names, comma separated)")
                yield Input(", ".join(plan.providers), id="providers")
                yield Checkbox("Chat in this sandbox (needs tongs-agents in the image)",
                               plan.chat, id="chat")
            with VerticalScroll(id="step-review"):
                yield Static("", id="policy", classes="text")
                yield Label("Policy file")
                yield Input("", id="policy-path")
                with Horizontal(id="review-buttons"):
                    yield Button("Save policy", id="save")
                    yield Button("Probe image (starts a throwaway sandbox)", id="probe")
                yield Static("", id="review-status", classes="text")
            with VerticalScroll(id="step-finish"):
                yield Static("", id="finish", classes="text")
        yield Static("", id="wizard-error")
        with Horizontal(id="wizard-buttons"):
            yield Button("Back", id="back")
            yield Button("Next", id="next", variant="primary")
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = "Sandbox setup"
        self._show(STEPS[0])
        self.check_host()

    # Host

    def _gateway(self) -> str | None:
        return self.query_one("#gateway", Input).value.strip() or None

    def check_host(self) -> None:
        """Validate the gateway field, then run the host checks; a bad name runs nothing."""
        gateway = self._gateway()
        field = self.query_one("#gateway-error", Static)
        if gateway is not None and not SANDBOX_TOKEN_RE.match(gateway):
            field.update(Text("A gateway name: letters, digits, '.', '_' or '-', "
                              "starting with a letter or digit (at most 64)."))
            self.report = None
            self.query_one("#host-checks", Static).update(
                Text("Fix the gateway name, then press 'Check again'."))
            return
        field.update("")
        self.query_one("#host-checks", Static).update("Checking the host...")
        self._check_host(gateway)

    @work(thread=True, exclusive=True, group="host")
    def _check_host(self, gateway: str | None) -> None:
        try:
            report = self._inspect(gateway)
        except Exception as error:  # noqa: BLE001 - shown, never raised into the UI
            report = HostReport(checks=(Check("openshell", "error", str(error)),),
                                gateway=gateway)
        self.app.call_from_thread(self._show_report, report)

    def _show_report(self, report: HostReport) -> None:
        self.report = report
        self.query_one("#host-checks", Static).update(checks_text(report))

    # Navigation

    @property
    def step(self) -> str:
        return self.query_one(ContentSwitcher).current or STEPS[0]

    def _show(self, step: str) -> None:
        self.query_one(ContentSwitcher).current = step
        self.query_one("#wizard-title", Label).update(STEP_TITLES[step])
        self.query_one("#back", Button).label = "Cancel" if step == STEPS[0] else "Back"
        self.query_one("#next", Button).label = "Done" if step == STEPS[-1] else "Next"
        self._error("")

    def _error(self, message: str) -> None:
        self.query_one("#wizard-error", Static).update(Text(message))

    def action_back(self) -> None:
        index = STEPS.index(self.step)
        if index == 0:
            self.action_cancel()
            return
        self._show(STEPS[index - 1])

    def _refuse_while_probing(self) -> bool:
        """True (and says why) while the probe sandbox may still exist."""
        if self.probing is None:
            return False
        name, gateway = self.probing
        self._error(f"Wait for the image probe to finish: it deletes its sandbox {name} "
                    f"when it ends. If it is left behind, run: "
                    f"{setup.probe_delete_command(name, gateway)}")
        return True

    def action_cancel(self) -> None:
        if self._refuse_while_probing():
            return
        self.dismiss(None)

    def action_next(self) -> None:
        step = self.step
        if step == STEPS[-1]:
            if self._refuse_while_probing():
                return
            self.dismiss(self.result)
            return
        try:
            if step == "step-plan":
                self._plan()
            elif step == "step-network":
                self._enter_review()
            elif step == "step-review":
                self._enter_finish()
        except SetupError as error:
            self._error(str(error))
            return
        self._show(STEPS[STEPS.index(step) + 1])

    # Plan

    def _plan(self) -> Plan:
        """The answers so far, validated."""
        harness = self.query_one("#harness", Select).value
        image = self.query_one("#image", Input).value.strip() or None
        providers = tuple(name for name in SPLIT_RE.split(
            self.query_one("#providers", Input).value) if name)
        presets = tuple(key for key in setup.PRESETS
                        if self.query_one(f"#preset-{key}", Checkbox).value)
        policy_path = self.query_one("#policy-path", Input).value.strip()
        folder = self.query_one("#folder", Input).value.strip()
        return Plan(
            profile=self.query_one("#profile", Input).value.strip(),
            harness=harness if isinstance(harness, str) else self.start.harness,
            folder=Path(folder) if folder else None,
            presets=presets,
            providers=providers,
            image=image,
            gateway=self._gateway(),
            chat=self.query_one("#chat", Checkbox).value,
            policy_path=Path(policy_path) if policy_path else None,
        ).check()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id != "harness" or not isinstance(event.value, str):
            return
        old, new = self._harness, event.value
        self._harness = new
        if old == new:
            return
        profile = self.query_one("#profile", Input)
        if profile.value in ("", f"{old}-boxed"):
            profile.value = f"{new}-boxed"
        old_preset, new_preset = setup.HARNESS_PRESET.get(old), setup.HARNESS_PRESET.get(new)
        if old_preset:
            self.query_one(f"#preset-{old_preset}", Checkbox).value = False
        if new_preset:
            self.query_one(f"#preset-{new_preset}", Checkbox).value = True
        providers = self.query_one("#providers", Input)
        names = [name for name in SPLIT_RE.split(providers.value) if name]
        old_name = setup.PRESETS[old_preset].provider if old_preset else None
        new_name = setup.PRESETS[new_preset].provider if new_preset else None
        names = [name for name in names if name != old_name]
        if new_name and new_name not in names:
            names.insert(0, new_name)
        providers.value = ", ".join(names)

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        key = (event.checkbox.id or "").removeprefix("preset-")
        preset = setup.PRESETS.get(key)
        if preset is None or not preset.provider or not event.value:
            return
        providers = self.query_one("#providers", Input)
        names = [name for name in SPLIT_RE.split(providers.value) if name]
        if preset.provider not in names:
            providers.value = ", ".join(names + [preset.provider])

    # Review

    def _enter_review(self) -> None:
        path = self.query_one("#policy-path", Input)
        plan = self._plan()
        if not path.value.strip():
            path.value = str(plan.resolved_policy_path())
            plan = self._plan()
        self.reviewed = setup.review(plan)
        self.policy_written = False
        self.query_one("#policy", Static).update(Text(self.reviewed.text))
        checked = self.report is not None and self.report.gateway == plan.gateway
        ready = checked and self.report.ready
        self.query_one("#probe", Button).disabled = not ready
        if ready:
            status = "Read the policy above; the probe starts a sandbox with exactly this policy."
        elif not checked:
            status = "The gateway changed since the host check; press 'Check again' in step 1."
        else:
            status = "The probe needs a reachable gateway (see step 1)."
        self.query_one("#review-status", Static).update(Text(status))

    def _reviewed_plan(self) -> Plan:
        plan = self._plan()
        if self.reviewed is None or not self.reviewed.matches(plan):
            raise SetupError("the policy changed since it was shown; go back and review it")
        return plan

    def save_policy(self) -> None:
        try:
            plan = self._reviewed_plan()
            written = setup.write_policy(self.reviewed, plan.resolved_policy_path())
        except SetupError as error:
            self._error(str(error))
            return
        self.policy_written = True
        self.query_one("#review-status", Static).update(Text(f"Saved the policy as {written}."))

    def start_probe(self) -> None:
        try:
            plan = self._reviewed_plan()
        except SetupError as error:
            self._error(str(error))
            return
        name = setup.probe_name()
        self.probing = (name, plan.gateway)
        self.query_one("#probe", Button).disabled = True
        self.query_one("#review-status", Static).update(Text(
            f"Probing the image in the throwaway sandbox {name}; cancelling waits for it "
            f"to be deleted."))
        self.run_probe(plan, self.reviewed, name)

    @work(thread=True, exclusive=True, group="probe")
    def run_probe(self, plan: Plan, reviewed: ReviewedPolicy, name: str) -> None:
        try:
            result = self._probe(plan, reviewed, name=name)
        except Exception as error:  # noqa: BLE001 - shown, never raised into the UI
            message = Text(f"{error}\nIf the probe sandbox {name} is left behind, run: "
                           f"{setup.probe_delete_command(name, plan.gateway)}")
        else:
            verdict = "Image looks ready." if result.ok else "The image is missing something:"
            message = Text("\n".join([verdict, *result.lines()]))
        self.app.call_from_thread(self._probe_done, message)

    def _probe_done(self, message: Text) -> None:
        self.probing = None
        self._error("")
        self.query_one("#review-status", Static).update(message)
        self.query_one("#probe", Button).disabled = False

    # Finish

    def _enter_finish(self) -> None:
        plan = self._reviewed_plan()
        self.result = setup.finish(plan, self.report, self.reviewed,
                                   policy_written=self.policy_written).text()
        self.query_one("#finish", Static).update(Text(self.result))

    # Buttons

    def on_button_pressed(self, event: Button.Pressed) -> None:
        actions = {
            "next": self.action_next,
            "back": self.action_back,
            "recheck": self.check_host,
            "save": self.save_policy,
            "probe": self.start_probe,
        }
        action = actions.get(event.button.id or "")
        if action is not None:
            event.stop()
            action()


class SandboxWizardApp(App[str | None]):
    """``tongs-agents sandbox setup`` on a terminal; exits with the final text."""

    TITLE = "tongs-agents"

    def __init__(self, plan: Plan | None = None, **screen_options) -> None:
        super().__init__()
        self.wizard = SandboxWizardScreen(plan, **screen_options)

    def on_mount(self) -> None:
        self.push_screen(self.wizard, callback=self.exit)

    async def action_quit(self) -> None:
        """ctrl+q, which App binds with priority, cancels like the wizard's own cancel."""
        if self.screen is self.wizard:
            self.wizard.action_cancel()
            return
        self.exit()
