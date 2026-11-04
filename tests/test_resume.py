"""``tongs-agents resume``, its sidebar verb, palette entries and picker (#125).

Session records are synthetic and the multiplexer is a fake: nothing starts
a harness. Config dirs and transcripts live under a temporary HOME.
"""

import dataclasses
import json
import os
import time
from types import SimpleNamespace

import pytest
from textual.widgets import OptionList

from tests.test_command_line import _settle, _submit
from tests.test_run import _inside_zellij, _main
from tests.test_workspace_spawn import FakeMux, runtime_dir  # noqa: F401
from tongs_agents import cli, discovery, launches
from tongs_agents.commands import resume, run, workspace
from tongs_agents.commands.sidebar import SidebarApp
from tongs_agents.models import UNRESOLVED_INSTANCE_ID, canonical_session_id
from tongs_agents.multiplexers import PaneRef
from tongs_agents.palette import COMMAND_LINE, RESUME_SELECTED, RESUME_SESSION
from tongs_agents.profiles import Profile, load_profiles
from tongs_agents.sessions_feed import WorkspaceFeed
from tongs_agents.widgets import agents_panel, command_line
from tongs_agents.widgets.chat_panel import ConfirmScreen
from tongs_agents.widgets.resume_picker import ResumePickerScreen

SESSION = "agents"
SIZE = (100, 30)
CLAUDE_ID = "3f2a9c1e-0000-4000-8000-000000000001"
CODEX_ID = "019a1b2c-0000-7000-8000-000000000002"
PI_ID = "5c6d7e8f-0000-4000-8000-000000000003"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for name in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "PI_CODING_AGENT_DIR",
                 "ZELLIJ", "ZELLIJ_SESSION_NAME", "ZELLIJ_PANE_ID", "TMUX", "TMUX_PANE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(agents_panel.shutil, "which", lambda name: None)
    return home


@pytest.fixture
def project(tmp_path):
    folder = tmp_path / "src" / "app"
    folder.mkdir(parents=True)
    return folder


def _profiles(tmp_path, text):
    config = tmp_path / "config"
    config.mkdir(mode=0o700, exist_ok=True)
    path = config / "config.toml"
    path.write_text(text)
    path.chmod(0o600)
    return load_profiles()


def _record(harness, session_id, cwd, *, status="idle", ts=None, pid="", source="hook",
            profile=None, env=None, instance=UNRESOLVED_INSTANCE_ID):
    record = {
        "schema_version": 1,
        "session_id": session_id,
        "instance_id": instance,
        "canonical_id": canonical_session_id(harness, session_id, instance),
        "harness_id": harness,
        "agent": harness,
        "repo": "app",
        "cwd": str(cwd),
        "pid": pid,
        "status": status,
        "detail": "",
        "ts": int(time.time()) if ts is None else ts,
        "source": source,
        "terminal": "konsole" if env is None else "zellij",
        "terminal_env": env or {},
    }
    if profile is not None:
        record["launch"] = {"profile_id": profile}
    return record


def _store(monkeypatch, records):
    monkeypatch.setattr(resume.discovery, "read_session_records", lambda: list(records))


def _claude_transcript(home, folder, session_id=CLAUDE_ID):
    directory = home / "projects" / resume._claude_project(str(folder))
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{session_id}.jsonl").write_text("{}\n")


def _codex_transcript(home, session_id=CODEX_ID):
    directory = home / "sessions" / "2026" / "11" / "02"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"rollout-2026-11-02T10-00-00-{session_id}.jsonl").write_text("{}\n")


# -- Listing the store --

def test_recent_sessions_lists_harness_sessions_newest_first(project):
    now = 1_000_000
    records = [
        _record("claude", CLAUDE_ID, project, ts=now - 300, status="ended"),
        _record("claude", CLAUDE_ID, project, ts=now - 30, instance="9:1"),
        _record("codex", CODEX_ID, project, ts=now - 60),
        _record("pi", PI_ID, project, ts=now - 600, profile="pi-main"),
        _record("claude", "proc-77", project, source="process"),
        _record("other", "abc", project),
        _record("claude", "-rf", project),
    ]
    sessions = resume.recent_sessions(records, now=now)
    assert [(s.harness, s.session_id, s.status, s.age_seconds) for s in sessions] == [
        ("claude", CLAUDE_ID, "idle", 30),
        ("codex", CODEX_ID, "idle", 60),
        ("pi", PI_ID, "idle", 600),
    ]
    assert len(sessions[0].records) == 2
    assert sessions[2].profile_id == "pi-main"


def test_recent_sessions_reads_the_real_store_without_writing(project):
    record = _record("claude", CLAUDE_ID, project)
    assert discovery.write_session_record(dict(record))
    [found] = resume.recent_sessions()
    assert (found.harness, found.session_id, found.folder) == ("claude", CLAUDE_ID, str(project))


def test_find_session_takes_an_exact_id_or_a_unique_prefix(project):
    sessions = resume.recent_sessions([
        _record("claude", CLAUDE_ID, project),
        _record("claude", "3f2b0000", project),
        _record("codex", CODEX_ID, project),
    ])
    assert resume.find_session(sessions, "019a").session_id == CODEX_ID
    assert resume.find_session(sessions, CLAUDE_ID).session_id == CLAUDE_ID
    with pytest.raises(resume.ResumeRefused, match="matches claude 3f2a.*claude 3f2b"):
        resume.find_session(sessions, "3f2")
    with pytest.raises(resume.ResumeRefused, match="no session 'zz'"):
        resume.find_session(sessions, "zz")


def test_resume_arguments_per_harness():
    assert resume.resume_args("claude", "x") == ("--resume", "x")
    assert resume.resume_args("codex", "x") == ("resume", "x")
    assert resume.resume_args("pi", "x") == ("--session", "x")
    assert "other" not in resume.RESUME_ARGS


# -- Transcripts and profiles --

def test_holds_session_finds_each_harness_transcript(tmp_path, project):
    claude, codex, pi = tmp_path / "c", tmp_path / "x", tmp_path / "p"
    sessions = {s.harness: s for s in resume.recent_sessions([
        _record("claude", CLAUDE_ID, project), _record("codex", CODEX_ID, project),
        _record("pi", PI_ID, project),
    ])}
    assert not resume.holds_session("claude", claude, sessions["claude"])
    _claude_transcript(claude, tmp_path / "elsewhere")
    assert resume.holds_session("claude", claude, sessions["claude"])
    _codex_transcript(codex)
    assert resume.holds_session("codex", codex, sessions["codex"])
    assert not resume.holds_session("codex", claude, sessions["codex"])
    (pi / "sessions" / "--src-app--").mkdir(parents=True)
    (pi / "sessions" / "--src-app--" / f"2026-11-02T10-00-00_{PI_ID}.jsonl").write_text("")
    assert resume.holds_session("pi", pi, sessions["pi"])


def test_the_profile_whose_config_dir_holds_the_session_is_picked(tmp_path, project, _isolated):
    alt = tmp_path / "claude-alt"
    profiles = _profiles(tmp_path, (
        '[profiles.main]\ncommand = ["claude"]\n'
        f'[profiles.alt]\ncommand = ["claude"]\nconfig_dir = {json.dumps(str(alt))}\n'
        '[profiles.cx]\ncommand = ["codex"]\n'
    ))
    [session] = resume.recent_sessions([_record("claude", CLAUDE_ID, project)])
    with pytest.raises(resume.ResumeRefused, match="no Claude Code profile's config dir.*"
                                                   "--profile NAME"):
        resume.match_profile(session, profiles)
    _claude_transcript(alt, project)
    assert resume.match_profile(session, profiles).name == "alt"
    assert resume.origin(session, profiles) == "alt"
    _claude_transcript(_isolated / ".claude", project)
    with pytest.raises(resume.ResumeRefused, match=r"several Claude Code .*\(alt, main\)"):
        resume.match_profile(session, profiles)


def test_profiles_sharing_the_config_dir_are_ambiguous(tmp_path, project, _isolated):
    profiles = _profiles(tmp_path, (
        '[profiles.one]\ncommand = ["claude"]\n[profiles.two]\ncommand = ["claude"]\n'
    ))
    _claude_transcript(_isolated / ".claude", project)
    [session] = resume.recent_sessions([_record("claude", CLAUDE_ID, project)])
    with pytest.raises(resume.ResumeRefused,
                       match=r"several Claude Code profiles .*\(one, two\); pass --profile"):
        resume.match_profile(session, profiles)
    assert resume.match_profile(session, profiles, "two").name == "two"
    assert resume.origin(session, profiles) == "~/.claude"


def test_named_and_recorded_profiles(tmp_path, project):
    profiles = _profiles(tmp_path, (
        '[profiles.one]\ncommand = ["claude"]\n[profiles.cx]\ncommand = ["codex"]\n'
    ))
    [session] = resume.recent_sessions([_record("claude", CLAUDE_ID, project, profile="one")])
    assert resume.match_profile(session, profiles).name == "one"
    with pytest.raises(resume.ResumeRefused, match="profile cx runs codex"):
        resume.match_profile(session, profiles, "cx")
    with pytest.raises(resume.ResumeRefused, match="unknown profile 'nope'"):
        resume.match_profile(session, profiles, "nope")
    [codex] = resume.recent_sessions([_record("codex", CODEX_ID, project)])
    with pytest.raises(resume.ResumeRefused, match="no Codex profile's config dir"):
        resume.match_profile(codex, profiles)
    with pytest.raises(resume.ResumeRefused, match="no Codex profile to resume it with"):
        resume.match_profile(codex, {"one": profiles["one"]})


def test_plan_adds_the_harness_resume_arguments_in_the_recorded_folder(tmp_path, project):
    profiles = _profiles(tmp_path, '[profiles.cx]\ncommand = ["codex"]\n')
    _codex_transcript(tmp_path / "home" / ".codex")
    [session] = resume.recent_sessions([_record("codex", CODEX_ID, project)])
    plan = resume.plan(session, profiles)
    assert plan.profile.name == "cx"
    assert plan.options == {"folder": str(project), "args": ("resume", CODEX_ID)}
    [gone] = resume.recent_sessions([_record("codex", CODEX_ID, tmp_path / "gone")])
    with pytest.raises(resume.ResumeRefused, match="does not exist"):
        resume.plan(gone, profiles)


# -- Live processes --

def test_a_live_process_is_warned_about_and_ended_records_are_skipped(project, monkeypatch):
    seen = []

    def resolve(record):
        seen.append(record["status"])
        return SimpleNamespace(pid=4242)

    monkeypatch.setattr(resume.discovery, "resolve_session_process", resolve)
    [ended] = resume.recent_sessions([_record("claude", CLAUDE_ID, project, status="ended")])
    assert resume.live_warning(ended) is None
    assert seen == []
    [live] = resume.recent_sessions([_record("claude", CLAUDE_ID, project, pid="4242")])
    warning = resume.live_warning(live)
    assert "still runs as pid 4242 in konsole" in warning
    assert "it was not stopped" in warning
    [here] = resume.recent_sessions([_record(
        "claude", CLAUDE_ID, project, pid="4242", env={"ZELLIJ_SESSION_NAME": SESSION},
    )])
    assert "in this workspace" in resume.live_warning(here, SESSION)
    assert "in session 'agents'" in resume.live_warning(here, "other")
    monkeypatch.setattr(resume.discovery, "resolve_session_process", lambda record: None)
    assert resume.live_warning(live) is None


def test_a_process_in_this_workspace_wins_over_one_elsewhere(project, monkeypatch):
    monkeypatch.setattr(resume.discovery, "resolve_session_process",
                        lambda record: SimpleNamespace(pid=int(record["pid"])))
    [session] = resume.recent_sessions([
        _record("claude", CLAUDE_ID, project, pid="10", ts=200),
        _record("claude", CLAUDE_ID, project, pid="20", ts=100, instance="9:1",
                env={"ZELLIJ_SESSION_NAME": SESSION}),
    ])
    live = resume.live_process(session, SESSION, "zellij")
    assert (live.pid, live.here) == (20, True)
    elsewhere = resume.live_process(session, "other")
    assert (elsewhere.pid, elsewhere.here, elsewhere.where) == (10, False, "in konsole")


def _codex_session(project, **times):
    return resume.recent_sessions([dict(_record("codex", CODEX_ID, project), **times)])[0]


def test_codex_days_cover_utc_and_local_dates_around_the_record(project, monkeypatch):
    # UTC+14: 08:00 local on 2 November is 18:00 UTC on 1 November.
    monkeypatch.setenv("TZ", "Pacific/Kiritimati")
    time.tzset()
    try:
        stamp = int(time.mktime(time.strptime("2026-11-02 08:00", "%Y-%m-%d %H:%M")))
        days = resume.codex_days(_codex_session(project, ts=stamp, started=stamp))
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()
    assert days == [("2026", "11", "01"), ("2026", "11", "02"), ("2026", "10", "31"),
                    ("2026", "11", "03")]


def test_codex_lookup_probes_the_records_days_before_any_walk(tmp_path, project,
                                                              monkeypatch):
    home = tmp_path / "codex"
    stamp = int(time.mktime(time.strptime("2026-11-02 12:00", "%Y-%m-%d %H:%M")))
    session = _codex_session(project, ts=stamp, started=stamp)
    _codex_transcript(home)
    for day in range(1, 29):
        (home / "sessions" / "2026" / "10" / f"{day:02d}").mkdir(parents=True)
    monkeypatch.setattr(resume, "MAX_SCAN_ENTRIES", 3)
    assert resume.holds_session("codex", home, session)
    old = _codex_session(project, ts=1, started=1)
    assert not resume.holds_session("codex", home, old)


def test_codex_walk_goes_newest_first_within_its_budget(tmp_path, project, monkeypatch):
    home = tmp_path / "codex"
    for month in ("01", "02", "03"):
        for day in ("01", "02"):
            folder = home / "sessions" / "2025" / month / day
            folder.mkdir(parents=True)
            (folder / "rollout-x-other.jsonl").write_text("")
    newest = home / "sessions" / "2025" / "03" / "02"
    (newest / f"rollout-2025-03-02T00-00-00-{CODEX_ID}.jsonl").write_text("")
    session = _codex_session(project, ts=1, started=1)
    walked = list(resume._newest_dirs(home / "sessions", 3, [100]))
    assert walked[:3] == [newest, newest.parent / "01", home / "sessions" / "2025" / "02" / "02"]
    # One year, three months and two days are listed before the newest day's two files.
    monkeypatch.setattr(resume, "MAX_SCAN_ENTRIES", 8)
    assert resume.holds_session("codex", home, session)
    oldest = home / "sessions" / "2025" / "01" / "01"
    (newest / f"rollout-2025-03-02T00-00-00-{CODEX_ID}.jsonl").rename(
        oldest / f"rollout-2025-01-01T00-00-00-{CODEX_ID}.jsonl")
    assert not resume.holds_session("codex", home, session)
    monkeypatch.setattr(resume, "MAX_SCAN_ENTRIES", 100)
    assert resume.holds_session("codex", home, session)


# -- The CLI --

def test_without_an_id_it_lists_the_sessions(tmp_path, project, monkeypatch, capsys):
    _profiles(tmp_path, '[profiles.cx]\ncommand = ["codex"]\n')
    _codex_transcript(tmp_path / "home" / ".codex")
    _store(monkeypatch, [_record("codex", CODEX_ID, project, ts=int(time.time()) - 90)])
    assert _main("resume") == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == ["HARNESS", "SESSION", "PROFILE/CONFIG", "STATUS", "AGE", "FOLDER"]
    assert lines[1].split() == ["codex", CODEX_ID, "cx", "idle", "1m", str(project)]
    assert "tongs-agents resume ID" in lines[-1]


def test_an_empty_store_lists_nothing(monkeypatch, capsys):
    _store(monkeypatch, [])
    assert _main("resume") == 0
    assert "No sessions to resume" in capsys.readouterr().out


def test_resume_inside_zellij_starts_the_session_through_run(tmp_path, project, monkeypatch,
                                                            capsys):
    _profiles(tmp_path, '[profiles.main]\ncommand = ["claude"]\n')
    _claude_transcript(tmp_path / "home" / ".claude", project)
    _store(monkeypatch, [_record("claude", CLAUDE_ID, project, pid="4242")])
    monkeypatch.setattr(resume.discovery, "resolve_session_process",
                        lambda record: SimpleNamespace(pid=4242))
    mux = FakeMux()
    monkeypatch.setattr(run, "get_multiplexer", lambda name="zellij", **kwargs: mux)
    _inside_zellij(monkeypatch, mux, SESSION)
    assert _main("resume", "3f2a") == 0
    [record] = launches.list_records()
    assert (record.profile, record.session, record.folder, record.args) == (
        "main", SESSION, str(project), ("--resume", CLAUDE_ID),
    )
    err = capsys.readouterr().err
    assert "warning: session 3f2a9c1e still runs as pid 4242" in err
    assert "resuming Claude Code session 3f2a9c1e with profile main" in err


def test_refusals_exit_2_and_start_nothing(tmp_path, project, monkeypatch, capsys):
    _profiles(tmp_path, '[profiles.a]\ncommand = ["claude"]\n[profiles.b]\ncommand = ["claude"]\n')
    _claude_transcript(tmp_path / "home" / ".claude", project)
    _store(monkeypatch, [_record("claude", CLAUDE_ID, project)])
    mux = FakeMux()
    monkeypatch.setattr(run, "get_multiplexer", lambda name="zellij", **kwargs: mux)
    _inside_zellij(monkeypatch, mux, SESSION)
    assert _main("resume", "3f2a") == 2
    assert "pass --profile NAME" in capsys.readouterr().err
    assert _main("resume", "zz") == 2
    assert _main("resume", "3f2a", "--profile", "b") == 0
    assert [record.profile for record in launches.list_records()] == ["b"]
    assert mux.spawned


def test_a_session_already_in_the_workspace_is_refused(tmp_path, project, monkeypatch,
                                                       capsys):
    _profiles(tmp_path, '[profiles.main]\ncommand = ["claude"]\n')
    _claude_transcript(tmp_path / "home" / ".claude", project)
    _store(monkeypatch, [_record("claude", CLAUDE_ID, project, pid="4242",
                                 env={"ZELLIJ_SESSION_NAME": SESSION})])
    monkeypatch.setattr(resume.discovery, "resolve_session_process",
                        lambda record: SimpleNamespace(pid=4242))
    mux = FakeMux()
    monkeypatch.setattr(run, "get_multiplexer", lambda name="zellij", **kwargs: mux)
    _inside_zellij(monkeypatch, mux, SESSION)
    assert _main("resume", "3f2a") == 2
    err = capsys.readouterr().err
    assert "already runs in this workspace as pid 4242" in err
    assert f"tongs-agents focus {CLAUDE_ID}" in err
    assert not mux.spawned and launches.list_records() == []
    _inside_zellij(monkeypatch, mux, "other")
    assert _main("resume", "3f2a") == 0
    assert "warning: session 3f2a9c1e still runs as pid 4242 in session 'agents'" in (
        capsys.readouterr().err)


def test_inside_tmux_the_warning_names_this_workspace(tmp_path, project, monkeypatch,
                                                      capsys):
    _profiles(tmp_path, '[profiles.main]\ncommand = ["claude"]\n')
    _claude_transcript(tmp_path / "home" / ".claude", project)
    record = launches.create_pending(load_profiles()["main"], SESSION, "tmux")
    row = _record("claude", CLAUDE_ID, project, pid="4242")
    row["launch"] = {"launch_id": record.launch_id}
    _store(monkeypatch, [row])
    monkeypatch.setattr(resume.discovery, "resolve_session_process",
                        lambda record: SimpleNamespace(pid=4242))
    here = PaneRef("tmux", SESSION, "%3", "/tmp/tmux-1000/default")
    monkeypatch.setattr(resume, "get_multiplexer",
                        lambda name: SimpleNamespace(in_session=lambda: here))
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,1,0")
    [session] = resume.recent_sessions()
    args = SimpleNamespace(session=None, multiplexer=None)
    settings = SimpleNamespace(session="ignored", multiplexer="zellij")
    assert resume._workspace_here(args, settings) == (SESSION, "tmux")
    live = resume.live_process(session, SESSION, "tmux")
    assert live.here and live.where == "in this workspace"
    assert not resume.live_process(session, SESSION, "zellij").here
    monkeypatch.setattr(resume, "get_multiplexer",
                        lambda name: SimpleNamespace(in_session=lambda: None))
    assert resume._workspace_here(args, settings) == (None, "tmux")


def test_resume_is_a_lazy_verb_with_static_metadata():
    entry = next(item for item in cli.commands.discover()
                 if cli.commands.command_name(item) == "resume")
    assert isinstance(entry, cli.commands.LazyCommand)
    assert entry.HELP == resume.HELP
    assert not hasattr(resume, "PASSTHROUGH")


def test_windows_is_refused(monkeypatch, capsys):
    monkeypatch.setattr(workspace, "_is_windows", lambda: True)
    assert _main("resume") == 2
    assert "unsupported on Windows" in capsys.readouterr().err


# -- The sidebar --

def _sidebar(mux, rows=()):
    feed = WorkspaceFeed(SESSION, mux, lambda: [discovery.annotate_session(dict(row), 0)
                                                for row in rows])
    return SidebarApp(mux, SESSION, feed=feed, refresh_interval=60)


def test_the_command_line_registers_resume_with_the_cli_parser():
    verb = command_line.VERBS["resume"]
    assert verb.add_arguments is resume.add_arguments
    assert not verb.passthrough


@pytest.mark.asyncio
async def test_the_resume_verb_lists_and_starts_sessions(tmp_path, project, monkeypatch):
    _profiles(tmp_path, '[profiles.cx]\ncommand = ["codex"]\n')
    _codex_transcript(tmp_path / "home" / ".codex")
    _store(monkeypatch, [_record("codex", CODEX_ID, project)])
    mux = FakeMux()
    app = _sidebar(mux)
    async with app.run_test(size=SIZE) as pilot:
        await _settle(app, pilot)
        app.action_command_line()
        await pilot.pause()
        line = app.command_line
        await _submit(app, pilot, "resume")
        assert CODEX_ID in line.result_text and "cx" in line.result_text
        await _submit(app, pilot, "resume 019a --session other")
        assert "refusing to use --session 'other'" in line.result_text
        await _submit(app, pilot, "resume 019a")
        assert line.result_text == (
            f"resumed Codex session 019a1b2c with profile cx in {project}"
        )
    [record] = launches.list_records()
    assert (record.profile, record.session, record.args) == ("cx", SESSION, ("resume", CODEX_ID))


def _labelled_launch(name="reviewer"):
    old = launches.create_pending(Profile(name="cx", harness="codex", argv=("codex",)),
                                  SESSION, "zellij")
    launches.claim(old.launch_id, os.getpid(), "not-this-process",
                   PaneRef("zellij", SESSION, "9", None))
    return launches.set_label(old.launch_id, name)


def test_previous_label_reads_the_latest_launch_of_the_session(tmp_path, project):
    old = _labelled_launch()
    newer = _record("codex", CODEX_ID, project, ts=200)
    newer["launch"] = {"launch_id": "f" * 32, "profile_id": "cx"}
    older = _record("codex", CODEX_ID, project, ts=100)
    older["launch"] = {"launch_id": old.launch_id, "profile_id": "cx"}
    [session] = resume.recent_sessions([older, newer])
    assert resume.previous_label(session) == "reviewer"
    [plain] = resume.recent_sessions([_record("codex", CODEX_ID, project)])
    assert resume.previous_label(plain) is None


@pytest.mark.asyncio
async def test_resuming_a_renamed_agent_keeps_its_name(tmp_path, project, monkeypatch):
    _profiles(tmp_path, '[profiles.cx]\ncommand = ["codex"]\n')
    _codex_transcript(tmp_path / "home" / ".codex")
    old = _labelled_launch("fix #130")
    stored = _record("codex", CODEX_ID, project)
    stored["launch"] = {"launch_id": old.launch_id, "profile_id": "cx"}
    _store(monkeypatch, [stored])
    mux = FakeMux()
    app = _sidebar(mux)
    async with app.run_test(size=SIZE) as pilot:
        await _settle(app, pilot)
        app.action_command_line()
        await pilot.pause()
        await _submit(app, pilot, "resume 019a")
    [new] = [r for r in launches.list_records() if r.launch_id != old.launch_id]
    assert (new.profile, new.args, new.label) == ("cx", ("resume", CODEX_ID), "fix #130")
    assert [call["name"] for call in mux.spawned] == ["fix #130"]


def _notes(app, monkeypatch):
    notes = []
    monkeypatch.setattr(app, "notify", lambda message, **kwargs: notes.append(
        (kwargs.get("title"), message)))
    return notes


@pytest.mark.asyncio
async def test_u_refuses_a_row_that_runs_here_and_offers_its_pane(tmp_path, project,
                                                                  monkeypatch):
    _profiles(tmp_path, '[profiles.main]\ncommand = ["claude"]\n')
    _claude_transcript(tmp_path / "home" / ".claude", project)
    row = _record("claude", CLAUDE_ID, project, pid="4242",
                  env={"ZELLIJ_SESSION_NAME": SESSION})
    _store(monkeypatch, [row])
    monkeypatch.setattr(resume.discovery, "resolve_session_process",
                        lambda record: SimpleNamespace(pid=4242))
    mux = FakeMux()
    app = _sidebar(mux, [row])
    focused = []
    monkeypatch.setattr(app, "_do_focus", focused.append)
    async with app.run_test(size=SIZE) as pilot:
        await _settle(app, pilot)
        assert app.agents_panel.selected_session().session_id == CLAUDE_ID
        await pilot.press("R")
        await _settle(app, pilot)
        assert not isinstance(app.screen, ConfirmScreen)
        await pilot.press("u")
        await _settle(app, pilot)
        assert isinstance(app.screen, ConfirmScreen)
        assert "already runs in this workspace as pid 4242" in app.screen.message
        await pilot.press("y")
        await _settle(app, pilot)
        assert not app.resuming
    assert [session.session_id for session in focused] == [CLAUDE_ID]
    assert not mux.spawned
    assert launches.list_records() == []


@pytest.mark.asyncio
async def test_a_session_here_without_a_row_is_refused_with_a_notice(tmp_path, project,
                                                                    monkeypatch):
    _profiles(tmp_path, '[profiles.main]\ncommand = ["claude"]\n')
    _claude_transcript(tmp_path / "home" / ".claude", project)
    _store(monkeypatch, [_record("claude", CLAUDE_ID, project, pid="4242",
                                 env={"ZELLIJ_SESSION_NAME": SESSION})])
    monkeypatch.setattr(resume.discovery, "resolve_session_process",
                        lambda record: SimpleNamespace(pid=4242))
    mux = FakeMux()
    app = _sidebar(mux)
    notes = _notes(app, monkeypatch)
    async with app.run_test(size=SIZE) as pilot:
        await _settle(app, pilot)
        app.resume_in_workspace(CLAUDE_ID)
        await _settle(app, pilot)
        assert not isinstance(app.screen, ConfirmScreen)
    [(title, message)] = notes
    assert title == "Already running here"
    assert f"tongs-agents focus {CLAUDE_ID}" in message
    assert not mux.spawned


@pytest.mark.asyncio
async def test_a_session_running_elsewhere_needs_a_confirmation(tmp_path, project,
                                                               monkeypatch):
    _profiles(tmp_path, '[profiles.main]\ncommand = ["claude"]\n')
    _claude_transcript(tmp_path / "home" / ".claude", project)
    _store(monkeypatch, [_record("claude", CLAUDE_ID, project, pid="4242")])
    monkeypatch.setattr(resume.discovery, "resolve_session_process",
                        lambda record: SimpleNamespace(pid=4242))
    mux = FakeMux()
    app = _sidebar(mux)
    notes = _notes(app, monkeypatch)
    async with app.run_test(size=SIZE) as pilot:
        await _settle(app, pilot)
        app.resume_in_workspace(CLAUDE_ID)
        await _settle(app, pilot)
        assert isinstance(app.screen, ConfirmScreen)
        assert "still runs as pid 4242 in konsole" in app.screen.message
        app.resume_in_workspace(CLAUDE_ID)
        await _settle(app, pilot)
        assert notes == [("Resume", "Session 3f2a9c1e is already being resumed from this sidebar")]
        await pilot.press("n")
        await _settle(app, pilot)
        assert not mux.spawned and not app.resuming
        app.resume_in_workspace(CLAUDE_ID)
        await _settle(app, pilot)
        await pilot.press("y")
        await _settle(app, pilot)
        assert not app.resuming
    titles = [title for title, _ in notes]
    assert titles[-2:] == ["Resume", "Still running"]
    [record] = launches.list_records()
    assert record.args == ("--resume", CLAUDE_ID)
    assert len(mux.spawned) == 1


@pytest.mark.asyncio
async def test_a_second_request_while_one_runs_is_ignored(tmp_path, project, monkeypatch):
    _profiles(tmp_path, '[profiles.cx]\ncommand = ["codex"]\n')
    _codex_transcript(tmp_path / "home" / ".codex")
    _store(monkeypatch, [_record("codex", CODEX_ID, project)])
    mux = FakeMux()
    app = _sidebar(mux)
    notes = _notes(app, monkeypatch)
    async with app.run_test(size=SIZE) as pilot:
        await _settle(app, pilot)
        app.resume_in_workspace(CODEX_ID)
        app.resume_in_workspace(CODEX_ID)
        await _settle(app, pilot)
        assert not app.resuming
    assert notes[0] == ("Resume", "Session 019a1b2c is already being resumed from this sidebar")
    assert [title for title, _ in notes[1:]] == ["Resume"]
    assert len(mux.spawned) == 1
    [record] = launches.list_records()
    assert record.args == ("resume", CODEX_ID)


@pytest.mark.asyncio
async def test_a_failed_resume_clears_the_request(tmp_path, project, monkeypatch):
    _profiles(tmp_path, '[profiles.cx]\ncommand = ["codex"]\n')
    _store(monkeypatch, [_record("codex", CODEX_ID, project)])
    mux = FakeMux()
    app = _sidebar(mux)
    notes = _notes(app, monkeypatch)
    async with app.run_test(size=SIZE) as pilot:
        await _settle(app, pilot)
        app.resume_in_workspace(CODEX_ID)
        await _settle(app, pilot)
        assert not app.resuming
    [(title, message)] = notes
    assert title == "Resume failed" and "no Codex profile's config dir" in message
    assert not mux.spawned


@pytest.mark.asyncio
async def test_rows_without_a_harness_session_are_not_resumed(monkeypatch, project):
    row = _record("other", "abc", project, env={"ZELLIJ_SESSION_NAME": SESSION})
    _store(monkeypatch, [])
    mux = FakeMux()
    app = _sidebar(mux, [row])
    notes = []
    monkeypatch.setattr(app, "notify", lambda message, **kwargs: notes.append(message))
    async with app.run_test(size=SIZE) as pilot:
        await _settle(app, pilot)
        app.action_resume_selected()
        await _settle(app, pilot)
    assert notes == ["app: no harness session to resume yet"]
    assert not mux.spawned


@pytest.mark.asyncio
async def test_the_palette_picker_resumes_the_chosen_session(tmp_path, project, monkeypatch):
    _profiles(tmp_path, '[profiles.cx]\ncommand = ["codex"]\n[profiles.main]\n'
                        'command = ["claude"]\n')
    _codex_transcript(tmp_path / "home" / ".codex")
    _store(monkeypatch, [
        _record("claude", CLAUDE_ID, project, ts=100),
        _record("codex", CODEX_ID, project, ts=200),
    ])
    mux = FakeMux()
    app = _sidebar(mux)
    async with app.run_test(size=SIZE) as pilot:
        await _settle(app, pilot)
        titles = [command.title for command in app.palette_commands()]
        assert titles[titles.index(COMMAND_LINE) + 1:][:2] == [RESUME_SELECTED, RESUME_SESSION]
        command = next(c for c in app.palette_commands() if c.title == RESUME_SESSION)
        command.callback()
        await _settle(app, pilot)
        assert isinstance(app.screen, ResumePickerScreen)
        options = app.screen.query_one(OptionList)
        assert options.option_count == 2
        assert "codex" in str(options.get_option_at_index(0).prompt)
        await pilot.press("enter")
        await _settle(app, pilot)
        assert not isinstance(app.screen, ResumePickerScreen)
    [record] = launches.list_records()
    assert (record.profile, record.args) == ("cx", ("resume", CODEX_ID))


@pytest.mark.asyncio
async def test_the_picker_closes_with_escape_and_an_empty_store_only_notifies(monkeypatch):
    _store(monkeypatch, [])
    mux = FakeMux()
    app = _sidebar(mux)
    notes = []
    monkeypatch.setattr(app, "notify", lambda message, **kwargs: notes.append(message))
    async with app.run_test(size=SIZE) as pilot:
        await _settle(app, pilot)
        app.action_resume_session()
        await _settle(app, pilot)
        assert notes == ["No sessions to resume in the session store"]
        sessions = resume.recent_sessions([_record("pi", PI_ID, "/tmp")])
        app.push_screen(ResumePickerScreen(sessions, SESSION))
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, ResumePickerScreen)
    assert not mux.spawned


# -- Before the harness writes a record (B1) --

def _cx(tmp_path):
    _profiles(tmp_path, '[profiles.cx]\ncommand = ["codex"]\n')
    _codex_transcript(tmp_path / "home" / ".codex")


@pytest.mark.asyncio
async def test_u_twice_spawns_once(tmp_path, project, monkeypatch):
    _cx(tmp_path)
    _store(monkeypatch, [_record("codex", CODEX_ID, project)])
    mux = FakeMux()
    app = _sidebar(mux)
    notes = _notes(app, monkeypatch)
    async with app.run_test(size=SIZE) as pilot:
        await _settle(app, pilot)
        app.resume_in_workspace(CODEX_ID)
        await _settle(app, pilot)
        assert not app.resuming
        app.resume_in_workspace(CODEX_ID)
        await _settle(app, pilot)
    assert len(mux.spawned) == 1
    assert notes[-1][0] == "Already running here"
    assert "already runs in this workspace in a pane that is starting" in notes[-1][1]


@pytest.mark.asyncio
async def test_u_then_resume_on_the_command_line_spawns_once(tmp_path, project,
                                                            monkeypatch):
    _cx(tmp_path)
    _store(monkeypatch, [_record("codex", CODEX_ID, project)])
    mux = FakeMux()
    app = _sidebar(mux)
    _notes(app, monkeypatch)
    async with app.run_test(size=SIZE) as pilot:
        await _settle(app, pilot)
        assert app.resuming.acquire(CODEX_ID)
        app.action_command_line()
        await pilot.pause()
        await _submit(app, pilot, "resume 019a")
        assert app.command_line.result_text == (
            "session 019a1b2c is already being resumed from this sidebar")
        app.resuming.release(CODEX_ID)
        app.resume_in_workspace(CODEX_ID)
        await _settle(app, pilot)
        await _submit(app, pilot, "resume 019a")
        assert "already runs in this workspace in a pane that is starting" in (
            app.command_line.result_text)
    assert len(mux.spawned) == 1


@pytest.mark.asyncio
async def test_resume_twice_on_the_command_line_spawns_once(tmp_path, project, monkeypatch):
    _cx(tmp_path)
    _store(monkeypatch, [_record("codex", CODEX_ID, project)])
    mux = FakeMux()
    app = _sidebar(mux)
    notes = _notes(app, monkeypatch)
    async with app.run_test(size=SIZE) as pilot:
        await _settle(app, pilot)
        app.action_command_line()
        await pilot.pause()
        await _submit(app, pilot, "resume 019a")
        assert app.command_line.result_text.startswith("resumed Codex session 019a1b2c")
        await _submit(app, pilot, "resume 019a")
        assert "already runs in this workspace" in app.command_line.result_text
        assert not app.resuming
        app.resume_in_workspace(CODEX_ID)
        await _settle(app, pilot)
    assert notes[-1][0] == "Already running here"
    assert len(mux.spawned) == 1


def test_the_cli_refuses_a_resume_in_its_pending_window(tmp_path, project, monkeypatch,
                                                        capsys):
    _cx(tmp_path)
    _store(monkeypatch, [_record("codex", CODEX_ID, project)])
    mux = FakeMux()
    monkeypatch.setattr(run, "get_multiplexer", lambda name="zellij", **kwargs: mux)
    _inside_zellij(monkeypatch, mux, SESSION)
    assert _main("resume", "019a") == 0
    assert _main("resume", "019a") == 2
    assert "already runs in this workspace in a pane that is starting" in (
        capsys.readouterr().err)
    _inside_zellij(monkeypatch, mux, "other")
    assert _main("resume", "019a") == 0
    assert [call["name"] for call in mux.spawned].count("cx") == 2


def _resume_launch(tmp_path, session=SESSION, multiplexer="zellij", **changes):
    record = launches.create_pending(
        load_profiles()["cx"], session, multiplexer, args=("resume", CODEX_ID))
    if changes:
        record = launches._write(dataclasses.replace(record, **changes))
    return record


def test_resuming_launch_counts_pending_and_live_claimed_launches(tmp_path, project,
                                                                  monkeypatch):
    _cx(tmp_path)
    [session] = resume.recent_sessions([_record("codex", CODEX_ID, project)])
    monkeypatch.setattr(resume.discovery, "read_session_records", lambda: [])
    now = time.time()
    assert resume.resuming_launch(session, SESSION) is None
    pending = _resume_launch(tmp_path)
    found = resume.resuming_launch(session, SESSION, "zellij", now=now)
    assert (found.pid, found.here) == (None, True)
    assert resume.resuming_launch(session, SESSION, "tmux", now=now) is None
    assert resume.resuming_launch(session, "other", now=now) is None
    late = now + resume.PENDING_LAUNCH_SECONDS + 1
    assert resume.resuming_launch(session, SESSION, now=late) is None
    launches.discard(pending.launch_id)
    claimed = _resume_launch(tmp_path, state="running", pid=4242, process_start="77")
    monkeypatch.setattr(resume.launches, "alive", lambda record: record.pid == 4242)
    assert resume.resuming_launch(session, SESSION, now=late).pid == 4242
    monkeypatch.setattr(resume.launches, "alive", lambda record: False)
    assert resume.resuming_launch(session, SESSION, now=late) is None
    monkeypatch.setattr(resume.launches, "alive", lambda record: True)
    monkeypatch.setattr(resume.discovery, "read_session_records", lambda: [
        {"session_id": "other", "launch": {"launch_id": claimed.launch_id}}])
    assert resume.resuming_launch(session, SESSION, now=late) is None
    _resume_launch(tmp_path, args=("resume", "--last"))
    monkeypatch.setattr(resume.discovery, "read_session_records", lambda: [])
    assert resume.resuming_launch(session, SESSION, now=now).pid == 4242


@pytest.mark.asyncio
async def test_the_focus_offer_picks_the_live_row(tmp_path, project, monkeypatch):
    _profiles(tmp_path, '[profiles.main]\ncommand = ["claude"]\n')
    _claude_transcript(tmp_path / "home" / ".claude", project)
    ended = _record("claude", CLAUDE_ID, project, pid="1111", status="ended",
                    env={"ZELLIJ_SESSION_NAME": SESSION, "ZELLIJ_PANE_ID": "3"})
    live = _record("claude", CLAUDE_ID, project, pid="4242", instance="9:1",
                   env={"ZELLIJ_SESSION_NAME": SESSION, "ZELLIJ_PANE_ID": "4"})
    _store(monkeypatch, [ended, live])
    monkeypatch.setattr(resume.discovery, "resolve_session_process",
                        lambda record: SimpleNamespace(pid=int(record["pid"])))
    mux = FakeMux()
    app = _sidebar(mux, [ended, live])
    focused = []
    monkeypatch.setattr(app, "_do_focus", focused.append)
    async with app.run_test(size=SIZE) as pilot:
        await _settle(app, pilot)
        assert len([row for row in app.agents_panel.sessions
                    if row.session_id == CLAUDE_ID]) == 2
        app.resume_in_workspace(CLAUDE_ID)
        await _settle(app, pilot)
        await pilot.press("y")
        await _settle(app, pilot)
    assert [str(row.pid) for row in focused] == ["4242"]
