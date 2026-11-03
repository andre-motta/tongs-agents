"""The sidebar command line: parsing, completion, history and runs (headless)."""

import argparse
import json
import os

import pytest
from textual.widgets import Input

from tests.test_workspace_spawn import FakeMux, runtime_dir  # noqa: F401
from tongs_agents import launches
from tongs_agents.commands import run as run_command
from tongs_agents.commands.sidebar import SidebarApp
from tongs_agents.palette import COMMAND_LINE, RELOAD_PROFILES
from tongs_agents.sessions_feed import WorkspaceFeed
from tongs_agents.widgets import agents_panel, command_line
from tongs_agents.widgets.command_line import (
    VERBS,
    CommandError,
    CommandHelp,
    Prepared,
    Verb,
    complete_line,
    folder_candidates,
    parse_command,
    register_verb,
)

SESSION = "agents"
SIZE = (60, 30)


@pytest.fixture(autouse=True)
def _no_desktop_notifications(monkeypatch):
    monkeypatch.setattr(agents_panel.shutil, "which", lambda name: None)


@pytest.fixture
def projects(tmp_path, monkeypatch):
    start = tmp_path / "start"
    alpha_home = tmp_path / "alpha-home"
    start.mkdir()
    alpha_home.mkdir()
    (start / "app").mkdir()
    (start / "apple").mkdir()
    (start / "tools").mkdir()
    config = tmp_path / "config"
    config.mkdir(mode=0o700, exist_ok=True)
    path = config / "config.toml"
    path.write_text(
        '[profiles.alpha]\ncommand = ["claude"]\n'
        f"cwd = {json.dumps(str(alpha_home))}\n"
        '[profiles.alpine]\ncommand = ["claude"]\n'
        '[profiles.beta]\ncommand = ["codex"]\n'
    )
    path.chmod(0o600)
    monkeypatch.chdir(start)
    return start


def _app(mux):
    feed = WorkspaceFeed(SESSION, mux, lambda: [])
    return SidebarApp(mux, SESSION, feed=feed, refresh_interval=60)


async def _settle(app, pilot):
    await app.workers.wait_for_complete()
    await pilot.pause()
    await pilot.pause()


async def _submit(app, pilot, line):
    field = app.command_line.input
    field.value = line
    field.cursor_position = len(line)
    await pilot.press("enter")
    await _settle(app, pilot)


# -- Parsing --

def test_run_parses_like_the_cli_with_passthrough_arguments():
    verb, args = parse_command(
        "run alpha ~/src -- --resume 'two words' -- x", VERBS,
    )
    assert verb is VERBS["run"]
    assert (args.profile, args.folder, args.extra) == (
        "alpha", "~/src", ["--resume", "two words", "--", "x"],
    )
    _, bare = parse_command("run alpha", VERBS)
    assert (bare.folder, bare.extra, bare.session, bare.multiplexer) == (None, [], None, None)


@pytest.mark.parametrize(("line", "expected"), [
    ("ls -la", "unknown command 'ls'; commands: run (shell commands are not run)"),
    ("!make", "unknown command '!make'"),
    ("run alpha --resume", "run: unrecognized arguments: --resume"),
    ("run", "the following arguments are required: PROFILE"),
    ("run alpha 'open", "cannot split the line"),
    ("run --multiplexer screen alpha", "invalid choice: 'screen'"),
    ("   ", "type a command"),
])
def test_refused_lines_name_the_problem(line, expected):
    with pytest.raises(CommandError) as raised:
        parse_command(line, VERBS)
    assert expected in str(raised.value)


def test_usage_errors_include_the_cli_usage_line():
    with pytest.raises(CommandError) as raised:
        parse_command("run", VERBS)
    assert "usage: run [-h] [--session NAME] [--multiplexer {zellij,tmux}]" in str(raised.value)


def test_help_is_returned_not_printed(capsys):
    with pytest.raises(CommandHelp) as raised:
        parse_command("run -h", VERBS)
    assert "Everything after -- is appended" in str(raised.value)
    assert capsys.readouterr() == ("", "")


def test_the_registry_takes_a_new_verb_once(monkeypatch):
    monkeypatch.setattr(command_line, "VERBS", {})

    def add_arguments(parser):
        parser.add_argument("id")

    verb = register_verb(Verb("resume", "Resume", add_arguments,
                              lambda context, args: Prepared("x", lambda: "y")))
    assert command_line.VERBS == {"resume": verb}
    with pytest.raises(ValueError):
        register_verb(verb)
    _, args = parse_command("resume 7", command_line.VERBS)
    assert args == argparse.Namespace(id="7")
    with pytest.raises(CommandError, match="unrecognized arguments: more"):
        parse_command("resume 7 -- more", command_line.VERBS)


# -- Completion --

def _names():
    return ["alpha", "alpine", "beta"]


def test_completion_extends_verbs_profiles_and_folders(projects):
    assert complete_line("", VERBS, _names) == ("run ", [])
    assert complete_line("r", VERBS, _names) == ("run ", [])
    assert complete_line("run b", VERBS, _names) == ("run beta ", [])
    assert complete_line("run al", VERBS, _names) == ("run alp", ["alpha", "alpine"])
    assert complete_line("run alp", VERBS, _names) == ("run alp", ["alpha", "alpine"])
    assert complete_line("run beta t", VERBS, _names) == ("run beta tools" + os.sep, [])
    assert complete_line("run beta ap", VERBS, _names) == (
        "run beta app", ["app" + os.sep, "apple" + os.sep],
    )
    assert complete_line("run --session agents b", VERBS, _names) == (
        "run --session agents beta ", [],
    )


def test_completion_leaves_harness_arguments_options_and_quotes_alone(projects):
    for value in ("run beta app -- t", "run beta app x", "run -", "run 'al",
                  "ls a", "run --session ", "run beta \"ap"):
        assert complete_line(value, VERBS, _names) == (value, [])


def test_folder_candidates_follow_the_add_agent_rules(tmp_path, monkeypatch):
    for name in ("project-one", "project-two", ".hidden"):
        (tmp_path / name).mkdir()
    (tmp_path / "somefile").write_text("")
    base = str(tmp_path) + os.sep
    assert folder_candidates(base + "pro") == [
        base + "project-one" + os.sep, base + "project-two" + os.sep,
    ]
    assert folder_candidates(base + "some") == []
    assert folder_candidates(base + "h") == []
    assert folder_candidates(base + ".h") == [base + ".hidden" + os.sep]
    assert folder_candidates(base + "missing/x") == []
    monkeypatch.setenv("HOME", str(tmp_path))
    assert folder_candidates("~") == ["~" + os.sep]
    assert folder_candidates("~/project-o") == ["~/project-one" + os.sep]
    assert folder_candidates("~nobody-here") == []
    monkeypatch.setattr(command_line, "MAX_COMPLETION_ENTRIES", 0)
    assert folder_candidates(base + "pro") == []


# -- In the sidebar --

@pytest.mark.asyncio
async def test_colon_runs_a_profile_in_this_session_with_folder_and_args(projects, tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    mux = FakeMux()
    app = _app(mux)
    async with app.run_test(size=SIZE) as pilot:
        await _settle(app, pilot)
        await pilot.press("colon")
        await pilot.pause()
        line = app.command_line
        assert line.has_class("visible")
        assert app.focused is line.input
        await _submit(app, pilot, f"run alpha {target} -- --resume 'two words'")
        assert line.result_text == f"started alpha in {target} with --resume 'two words'"
        assert line.input.value == ""
        assert line.has_class("visible")
    [record] = launches.list_records()
    assert (record.profile, record.session, record.folder, record.args) == (
        "alpha", SESSION, str(target), ("--resume", "two words"),
    )
    assert [call["session"] for call in mux.spawned] == [SESSION]


@pytest.mark.asyncio
async def test_dir_defaults_to_the_sidebar_folder_and_relative_dirs_resolve_there(projects):
    mux = FakeMux()
    app = _app(mux)
    async with app.run_test(size=SIZE) as pilot:
        await _settle(app, pilot)
        app.action_command_line()
        await pilot.pause()
        await _submit(app, pilot, "run beta")
        await _submit(app, pilot, "run beta app")
    assert sorted(record.folder for record in launches.list_records()) == [
        str(projects), str(projects / "app"),
    ]


@pytest.mark.asyncio
async def test_refusals_are_shown_inline_and_start_nothing(projects, tmp_path):
    mux = FakeMux()
    app = _app(mux)
    async with app.run_test(size=SIZE) as pilot:
        await _settle(app, pilot)
        app.action_command_line()
        await pilot.pause()
        line = app.command_line
        cases = [
            ("run nope", "unknown profile 'nope'"),
            (f"run alpha {tmp_path / 'gone'}", "does not exist"),
            ("run alpha --session other", "refusing to use --session 'other'"),
            ("run alpha --multiplexer tmux", "refusing --multiplexer tmux"),
            ("rm -rf /", "unknown command 'rm'"),
        ]
        for text, expected in cases:
            await _submit(app, pilot, text)
            assert expected in line.result_text, text
            assert line.query_one(".command-result").has_class("error")
            assert line.input.value == text
        await _submit(app, pilot, "run alpha --session agents --multiplexer zellij")
        assert line.result_text.startswith("started alpha")
        assert not line.query_one(".command-result").has_class("error")
    assert [call["name"] for call in mux.spawned] == ["alpha"]


@pytest.mark.asyncio
async def test_a_failed_spawn_is_shown_inline(projects):
    mux = FakeMux(fail_spawn={"beta"})
    app = _app(mux)
    async with app.run_test(size=SIZE) as pilot:
        await _settle(app, pilot)
        app.action_command_line()
        await pilot.pause()
        await _submit(app, pilot, "run beta")
        assert app.command_line.result_text == "cannot spawn beta"
    assert launches.list_records() == []


@pytest.mark.asyncio
async def test_history_walks_submitted_lines_and_restores_the_draft(projects):
    app = _app(FakeMux())
    async with app.run_test(size=SIZE) as pilot:
        await _settle(app, pilot)
        app.action_command_line()
        await pilot.pause()
        line = app.command_line
        for text in ("run nope", "run alpha -h", "run nope"):
            await _submit(app, pilot, text)
        assert line.history == ["run nope", "run alpha -h", "run nope"]
        line.input.value = "draft"
        await pilot.press("up")
        assert line.input.value == "run nope"
        await pilot.press("up")
        assert line.input.value == "run alpha -h"
        await pilot.press("up", "up", "up")
        assert line.input.value == "run nope"
        assert line.input.cursor_position == len("run nope")
        await pilot.press("down", "down")
        assert line.input.value == "run nope"
        await pilot.press("down")
        assert line.input.value == "draft"
        await pilot.press("down")
        assert line.input.value == "draft"


@pytest.mark.asyncio
async def test_history_survives_closing_and_is_bounded(projects, monkeypatch):
    monkeypatch.setattr(command_line, "HISTORY_LIMIT", 2)
    app = _app(FakeMux())
    async with app.run_test(size=SIZE) as pilot:
        await _settle(app, pilot)
        app.action_command_line()
        await pilot.pause()
        for text in ("run a", "run b", "run c"):
            await _submit(app, pilot, text)
        await pilot.press("escape")
        await pilot.pause()
        line = app.command_line
        assert not line.has_class("visible")
        assert app.focused is app.agents_panel.table
        await pilot.press("colon")
        await pilot.pause()
        assert line.input.value == "" and line.result_text == ""
        assert line.history == ["run b", "run c"]
        await pilot.press("up")
        assert line.input.value == "run c"


@pytest.mark.asyncio
async def test_tab_completes_in_the_prompt_and_lists_candidates(projects):
    app = _app(FakeMux())
    async with app.run_test(size=SIZE) as pilot:
        await _settle(app, pilot)
        await pilot.press("colon")
        await pilot.pause()
        line = app.command_line
        await pilot.press("r", "u", "tab", "b", "tab", "t", "tab")
        assert line.input.value == "run beta tools" + os.sep
        assert app.focused is line.input
        line.input.value = "run al"
        line.input.cursor_position = len("run al")
        await pilot.press("tab")
        assert line.input.value == "run alp"
        assert line.result_text == "alpha  alpine"


@pytest.mark.asyncio
async def test_help_is_shown_under_the_prompt(projects):
    app = _app(FakeMux())
    async with app.run_test(size=SIZE) as pilot:
        await _settle(app, pilot)
        app.action_command_line()
        await pilot.pause()
        await _submit(app, pilot, "run -h")
        assert "usage: run" in app.command_line.result_text
        assert not app.command_line.query_one(".command-result").has_class("error")


@pytest.mark.asyncio
async def test_the_palette_offers_the_command_line_after_the_shared_entries(projects):
    app = _app(FakeMux())
    async with app.run_test(size=SIZE) as pilot:
        await _settle(app, pilot)
        titles = [command.title for command in app.palette_commands()]
        assert titles[titles.index(RELOAD_PROFILES) + 1] == COMMAND_LINE
        command = next(c for c in app.palette_commands() if c.title == COMMAND_LINE)
        command.callback()
        await pilot.pause()
        assert app.command_line.has_class("visible")
        assert isinstance(app.focused, Input)


def test_the_run_verb_shares_the_cli_parser_and_checks():
    verb = VERBS["run"]
    assert verb.add_arguments is run_command.add_arguments
    assert verb.passthrough
    assert verb.completions == (command_line.COMPLETE_PROFILE, command_line.COMPLETE_FOLDER)
