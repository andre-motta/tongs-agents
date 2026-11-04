"""Resuming past sessions from the harnesses' own history stores (#133).

Every store is a synthetic fixture under a temporary HOME; nothing reads a
real transcript and nothing starts a harness (the multiplexer is a fake).
"""

import dataclasses
import json
import os
import time
from types import SimpleNamespace

import pytest

from tests.test_command_line import _settle, _submit
from tests.test_resume import SESSION, SIZE, _profiles, _record, _sidebar, _store
from tests.test_run import _inside_zellij, _main
from tests.test_workspace_spawn import FakeMux, runtime_dir  # noqa: F401
from tongs_agents import history, launches
from tongs_agents.commands import resume, run
from tongs_agents.widgets.resume_picker import RUNNING_MARK, ResumePickerScreen

CLAUDE_ID = "7c1d0e2f-0000-4000-8000-00000000000a"
OTHER_ID = "7c1d0e2f-0000-4000-8000-00000000000b"
CODEX_ID = "01a1b2c3-0000-7000-8000-00000000000c"
PI_ID = "01a0c4d5-0000-7000-8000-00000000000d"
NOW = int(time.time()) - 3600


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for name in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "PI_CODING_AGENT_DIR",
                 "ZELLIJ", "ZELLIJ_SESSION_NAME", "ZELLIJ_PANE_ID", "TMUX", "TMUX_PANE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(resume.discovery, "read_session_records", lambda: [])
    return home


@pytest.fixture
def project(tmp_path):
    folder = tmp_path / "src" / "app"
    folder.mkdir(parents=True)
    return folder


def _lines(*records):
    return "".join(json.dumps(record) + "\n" for record in records)


def _touch(path, mtime):
    os.utime(path, (mtime, mtime))
    return path


def claude_session(home, session_id, cwd, *records, mtime=NOW, prompt="fix the build"):
    directory = home / "projects" / ("-" + str(cwd).strip("/").replace("/", "-"))
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session_id}.jsonl"
    path.write_text(_lines(
        {"type": "permission-mode", "sessionId": session_id},
        {"type": "user", "isMeta": True, "cwd": str(cwd), "sessionId": session_id,
         "message": {"role": "user", "content": "<local-command-caveat>x</local-command-caveat>"}},
        {"type": "user", "cwd": str(cwd), "sessionId": session_id, "gitBranch": "main",
         "timestamp": "2026-11-02T10:00:00Z",
         "message": {"role": "user", "content": [{"type": "text", "text": prompt}]}},
        *records,
    ))
    return _touch(path, mtime)


def codex_session(home, session_id, cwd, *records, mtime=NOW, source="cli",
                  day=("2026", "11", "02")):
    directory = home / "sessions" / day[0] / day[1] / day[2]
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"rollout-{'-'.join(day)}T10-00-00-{session_id}.jsonl"
    meta = {"type": "session_meta", "payload": {
        "id": session_id, "session_id": "unrelated-root", "cwd": str(cwd), "source": source,
        "timestamp": "2026-11-02T10:00:00Z", "cli_version": "0.166.0",
        "base_instructions": "x" * 2000,
    }}
    path.write_text(_lines(meta, *records))
    return _touch(path, mtime)


def codex_user(text):
    return {"type": "response_item", "payload": {
        "type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}}


def pi_session(home, session_id, cwd, *records, mtime=NOW, prompt="tidy the docs"):
    directory = home / "sessions" / ("--" + str(cwd).strip("/").replace("/", "-") + "--")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"2026-11-02T10-00-00-000Z_{session_id}.jsonl"
    path.write_text(_lines(
        {"type": "session", "version": 3, "id": session_id, "cwd": str(cwd),
         "timestamp": "2026-11-02T10:00:00.000Z"},
        {"type": "model_change", "id": "a1", "parentId": None, "modelId": "m"},
        {"type": "message", "id": "a2", "parentId": "a1",
         "message": {"role": "user", "content": [{"type": "text", "text": prompt}]}},
        *records,
    ))
    return _touch(path, mtime)


# -- Titles --

def test_clean_title_keeps_one_short_printable_line():
    assert history.clean_title("  fix\tthe\n\x1b[31mbuild\x07  ") == "fix the [31mbuild"
    long = history.clean_title("word " * 40)
    assert len(long) == history.TITLE_LENGTH and long.endswith(history.ELLIPSIS)
    assert history.clean_title(None) == "" and history.clean_title("​ ") == ""


def test_title_label_fits_a_launch_label():
    assert resume.title_label("-- fix it") == "fix it"
    assert resume.title_label("tongs-agents-home") is None
    assert resume.title_label("") is None
    assert len(resume.title_label("x" * 200)) == launches.MAX_LABEL_LENGTH


# -- Claude Code --

TITLE_ORDER = [
    ("last-prompt", "lastPrompt", "last thing typed"),
    ("summary", "summary", "a summary"),
    ("ai-title", "aiTitle", "first ai title"),
    ("ai-title", "aiTitle", "Fix the build"),
    ("agent-name", "agentName", "builder"),
    ("custom-title", "customTitle", "My build fix"),
]


def test_claude_title_precedence_and_folder(tmp_path, project):
    home = tmp_path / "claude"
    claude_session(home, CLAUDE_ID, project)
    [plain] = history.scan("claude", home)
    assert (plain.session_id, plain.folder, plain.title) == (
        CLAUDE_ID, str(project), "fix the build")
    for count in range(1, len(TITLE_ORDER) + 1):
        records = [{"type": kind, key: value, "sessionId": CLAUDE_ID}
                   for kind, key, value in TITLE_ORDER[:count]]
        claude_session(home, CLAUDE_ID, project, *records)
        [found] = history.scan("claude", home)
        assert found.title == TITLE_ORDER[count - 1][2]


def test_claude_skips_malformed_symlinked_and_nested_files(tmp_path, project):
    home = tmp_path / "claude"
    path = claude_session(home, CLAUDE_ID, project)
    with path.open("a") as handle:
        handle.write("{not json\n[1, 2]\n\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = claude_session(outside, OTHER_ID, project)
    (path.parent / f"{OTHER_ID}.jsonl").symlink_to(secret)
    (home / "projects" / "-linked").symlink_to(secret.parent)
    nested = path.parent / CLAUDE_ID / "subagents"
    nested.mkdir(parents=True)
    (nested / "agent-1.jsonl").write_text(_lines({"type": "user", "cwd": str(project)}))
    (path.parent / "notes.txt").write_text("x")
    (path.parent / "-bad id.jsonl").write_text("{}\n")
    assert [s.session_id for s in history.scan("claude", home)] == [CLAUDE_ID]


def test_claude_without_cwd_has_no_folder(tmp_path):
    home = tmp_path / "claude"
    directory = home / "projects" / "-gone"
    directory.mkdir(parents=True)
    (directory / f"{CLAUDE_ID}.jsonl").write_text(_lines(
        {"type": "ai-title", "aiTitle": "orphan", "sessionId": CLAUDE_ID}))
    [found] = history.scan("claude", home)
    assert (found.folder, found.title) == ("", "orphan")
    (directory / f"{OTHER_ID}.jsonl").write_text("")
    assert [s.session_id for s in history.scan("claude", home)] == [CLAUDE_ID]


def test_a_huge_transcript_is_read_only_at_its_ends(tmp_path, project, monkeypatch):
    home = tmp_path / "claude"
    filler = {"type": "assistant", "cwd": str(project),
              "message": {"role": "assistant", "content": "y" * 1000}}
    middle = {"type": "custom-title", "customTitle": "hidden in the middle"}
    path = claude_session(home, CLAUDE_ID, project, *([filler] * 3000), middle,
                          *([filler] * 3000), {"type": "ai-title", "aiTitle": "at the end"})
    assert path.stat().st_size > 5_000_000
    read = []
    opened = history._open

    class Counting:
        def __init__(self, target):
            self.handle = opened(target)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.handle.close()

        def read(self, size=-1):
            data = self.handle.read(size)
            read.append(len(data))
            return data

        def __getattr__(self, name):
            return getattr(self.handle, name)

    monkeypatch.setattr(history, "_open", Counting)
    [found] = history.scan("claude", home)
    assert found.title == "at the end"
    assert sum(read) <= history.HEAD_BYTES + history.TAIL_BYTES


# -- Codex --

def test_codex_lists_user_threads_with_their_names(tmp_path, project):
    home = tmp_path / "codex"
    codex_session(home, CODEX_ID, project, codex_user("<environment_context>x"),
                  codex_user("# AGENTS.md instructions for /x"), codex_user("ship the fix"))
    codex_session(home, "01a1b2c3-0000-7000-8000-0000000000ee", project,
                  source={"subagent": {"other": "guardian"}})
    other = "01a1b2c3-0000-7000-8000-0000000000ff"
    codex_session(home, other, project,
                  {"type": "event_msg", "payload": {"type": "user_message", "message": "hi"}},
                  mtime=NOW - 10)
    (home / "sessions" / "2026" / "11" / "02" / "rollout-x-bad.jsonl").write_text("{oops\n")
    found = history.scan("codex", home)
    assert [(s.session_id, s.title, s.folder) for s in found] == [
        (CODEX_ID, "ship the fix", str(project)), (other, "hi", str(project))]
    (home / "session_index.jsonl").write_text(_lines(
        {"id": CODEX_ID, "thread_name": "old name", "updated_at": "x"},
        {"id": CODEX_ID, "thread_name": "Ship\nthe fix", "updated_at": "y"}) + "{bad\n")
    assert history.scan("codex", home)[0].title == "Ship the fix"


def test_codex_skips_a_first_line_past_its_bound(tmp_path, project, monkeypatch):
    home = tmp_path / "codex"
    codex_session(home, CODEX_ID, project)
    monkeypatch.setattr(history, "FIRST_LINE_BYTES", 100)
    assert history.scan("codex", home) == []


# -- Pi --

def test_pi_takes_the_session_name_else_the_first_prompt(tmp_path, project):
    home = tmp_path / "pi"
    path = pi_session(home, PI_ID, project)
    [found] = history.scan("pi", home)
    assert (found.session_id, found.folder, found.title) == (PI_ID, str(project),
                                                             "tidy the docs")
    pi_session(home, PI_ID, project,
               {"type": "session_info", "id": "b1", "parentId": "a2", "name": "first"},
               {"type": "session_info", "id": "b2", "parentId": "b1", "name": "Docs pass"})
    assert history.scan("pi", home)[0].title == "Docs pass"
    path.rename(path.with_name("2026-11-02T10-00-00-000Z_mismatch.jsonl"))
    assert history.scan("pi", home) == []


# -- Order, caps and lookup --

def test_newest_first_within_the_caps_and_locate_past_them(tmp_path, project, monkeypatch):
    home = tmp_path / "claude"
    ids = [f"7c1d0e2f-0000-4000-8000-00000000010{index}" for index in range(5)]
    for index, session_id in enumerate(ids):
        claude_session(home, session_id, project, mtime=NOW + index)
    assert [s.session_id for s in history.scan("claude", home)] == ids[::-1]
    monkeypatch.setattr(history, "MAX_SESSIONS", 2)
    assert [s.session_id for s in history.scan("claude", home)] == ids[:2:-1]
    monkeypatch.setattr(history, "MAX_FILES", 1)
    assert [s.session_id for s in history.scan("claude", home)] == [ids[-1]]
    assert history.locate("claude", home, ids[0]).session_id == ids[0]
    assert history.locate("claude", home, "../x") is None
    monkeypatch.setattr(history, "MAX_ENTRIES", 1)
    assert len(history.transcripts("claude", home)) <= 1


def test_codex_days_are_walked_newest_first(tmp_path, project, monkeypatch):
    home = tmp_path / "codex"
    old = codex_session(home, CODEX_ID, project, day=("2025", "01", "01"), mtime=NOW + 99)
    new = codex_session(home, PI_ID, project, day=("2026", "11", "02"), mtime=NOW)
    assert [path for _, path in history.transcripts("codex", home)] == [old, new]
    assert [path for _, path in history.transcripts("codex", home, enough=1)] == [new]


# -- Merging with the store and choosing the profile --

def _three(tmp_path, project, _isolated):
    claude_session(_isolated / ".claude", CLAUDE_ID, project, mtime=NOW - 30,
                   prompt="claude work")
    codex_session(_isolated / ".codex", CODEX_ID, project, codex_user("codex work"),
                  mtime=NOW - 20)
    pi_session(_isolated / ".pi" / "agent", PI_ID, project, mtime=NOW - 10)
    return _profiles(tmp_path, (
        '[profiles.main]\ncommand = ["claude"]\n'
        '[profiles.cx]\ncommand = ["codex"]\n'
        '[profiles.pi]\ncommand = ["pi"]\n'
    ))


def test_all_sessions_merges_the_store_with_every_profiles_history(tmp_path, project,
                                                                   _isolated):
    profiles = _three(tmp_path, project, _isolated)
    stored = _record("claude", CLAUDE_ID, project, ts=NOW - 5, status="running")
    sessions = resume.all_sessions(profiles, [stored], now=NOW)
    assert [(s.harness, s.status, s.title, s.age_seconds) for s in sessions] == [
        ("claude", "running", "claude work", 5),
        ("pi", "", "tidy the docs", 10),
        ("codex", "", "codex work", 20),
    ]
    assert sessions[0].running and not sessions[1].running
    assert sessions[0].homes == (_isolated / ".claude",)
    assert resume.origins(sessions, profiles) == {
        ("claude", CLAUDE_ID): "main", ("pi", PI_ID): "pi", ("codex", CODEX_ID): "cx"}


def test_sandboxed_profiles_add_no_history(tmp_path, project, _isolated):
    profiles = _three(tmp_path, project, _isolated)
    assert [h for h, _ in resume.history_homes(profiles)] == ["claude", "codex", "pi"]
    assert resume.history_homes({}) == []


def test_a_shared_store_takes_the_profile_of_the_newest_resume(tmp_path, project, _isolated):
    claude_session(_isolated / ".claude", CLAUDE_ID, project)
    alt = tmp_path / "alt"
    claude_session(alt, OTHER_ID, project)
    profiles = _profiles(tmp_path, (
        '[profiles.one]\ncommand = ["claude"]\n[profiles.two]\ncommand = ["claude"]\n'
        f'[profiles.alt]\ncommand = ["claude"]\nconfig_dir = {json.dumps(str(alt))}\n'
        '[profiles.cx]\ncommand = ["codex"]\n'
    ))
    sessions = {s.session_id: s for s in resume.all_sessions(profiles)}
    assert resume.match_profile(sessions[OTHER_ID], profiles).name == "alt"
    target = sessions[CLAUDE_ID]
    with pytest.raises(resume.ResumeRefused, match=r"several .*\(one, two\); pass --profile"):
        resume.match_profile(target, profiles)
    assert resume.origin(target, profiles) == "~/.claude"
    for age, name in ((60, "one"), (30, "two")):
        record = launches.create_pending(profiles[name], SESSION, "zellij",
                                         args=resume.resume_args("claude", CLAUDE_ID))
        launches._write(dataclasses.replace(record, created=record.created - age))
    assert resume.match_profile(target, profiles).name == "two"
    assert resume.match_profile(target, profiles, "one").name == "one"
    with pytest.raises(resume.ResumeRefused, match="profile cx runs codex"):
        resume.match_profile(target, profiles, "cx")


def test_plan_takes_dir_refuses_missing_folders_and_labels_with_the_title(
        tmp_path, project, _isolated):
    profiles = _three(tmp_path, project, _isolated)
    sessions = {s.harness: s for s in resume.all_sessions(profiles)}
    plans = {harness: resume.plan(session, profiles) for harness, session in sessions.items()}
    assert {h: (p.profile.name, p.options["folder"], p.options["args"], p.label)
            for h, p in plans.items()} == {
        "claude": ("main", str(project), ("--resume", CLAUDE_ID), "claude work"),
        "codex": ("cx", str(project), ("resume", CODEX_ID), "codex work"),
        "pi": ("pi", str(project), ("--session", PI_ID), "tidy the docs"),
    }
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    assert resume.plan(sessions["pi"], profiles, folder=str(elsewhere)).options["folder"] == (
        str(elsewhere))
    project.rmdir()
    with pytest.raises(resume.ResumeRefused,
                       match=f"folder {project} of session 01a0c4d5 does not exist.*DIR"):
        resume.plan(sessions["pi"], profiles)
    homeless = resume.Resumable("claude", OTHER_ID, "", "", 0, 0)
    with pytest.raises(resume.ResumeRefused, match="no recorded folder; give the folder"):
        resume.plan(homeless, profiles)


def test_a_renamed_resume_label_wins_over_the_title(tmp_path, project, _isolated):
    profiles = _three(tmp_path, project, _isolated)
    old = launches.create_pending(profiles["cx"], SESSION, "zellij",
                                  args=("resume", CODEX_ID), label="reviewer")
    [codex] = [s for s in resume.all_sessions(profiles) if s.harness == "codex"]
    assert resume.plan(codex, profiles).label == "reviewer"
    launches.discard(old.launch_id)
    assert resume.plan(codex, profiles).label == "codex work"


def test_lookup_finds_a_full_id_past_the_listing(tmp_path, project, _isolated, monkeypatch):
    profiles = _three(tmp_path, project, _isolated)
    monkeypatch.setattr(history, "MAX_SESSIONS", 0)
    assert resume.all_sessions(profiles) == []
    found = resume.lookup(profiles, CODEX_ID)
    assert (found.harness, found.title, found.homes) == ("codex", "codex work",
                                                         (_isolated / ".codex",))
    with pytest.raises(resume.ResumeRefused, match="no session '01a1'"):
        resume.lookup(profiles, "01a1")


# -- The CLI --

def test_the_cli_lists_past_sessions_with_titles(tmp_path, project, _isolated, capsys):
    _three(tmp_path, project, _isolated)
    assert _main("resume") == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split()[-1] == "TITLE"
    rows = [line.split() for line in lines[1:4]]
    assert [row[:4] for row in rows] == [
        ["pi", PI_ID, "pi", "-"], ["codex", CODEX_ID, "cx", "-"],
        ["claude", CLAUDE_ID, "main", "-"]]
    assert rows[0][-3:] == ["tidy", "the", "docs"]


@pytest.mark.parametrize(("prefix", "args"), [
    ("7c1d", ("--resume", CLAUDE_ID)),
    ("01a1", ("resume", CODEX_ID)),
    ("01a0", ("--session", PI_ID)),
])
def test_the_cli_resumes_each_harness_from_history(tmp_path, project, _isolated, monkeypatch,
                                                   capsys, prefix, args):
    _three(tmp_path, project, _isolated)
    mux = FakeMux()
    monkeypatch.setattr(run, "get_multiplexer", lambda name="zellij", **kwargs: mux)
    _inside_zellij(monkeypatch, mux, SESSION)
    assert _main("resume", prefix) == 0
    [record] = launches.list_records()
    assert (record.folder, record.args) == (str(project), args)


def test_the_cli_takes_dir_and_refuses_missing_folders(tmp_path, project, _isolated,
                                                       monkeypatch, capsys):
    _three(tmp_path, project, _isolated)
    mux = FakeMux()
    monkeypatch.setattr(run, "get_multiplexer", lambda name="zellij", **kwargs: mux)
    _inside_zellij(monkeypatch, mux, SESSION)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(tmp_path)
    assert _main("resume", "01a0", "elsewhere", "--profile", "pi") == 0
    [record] = launches.list_records()
    assert (record.profile, record.folder) == ("pi", str(elsewhere))
    project.rmdir()
    assert _main("resume", "7c1d") == 2
    assert "does not exist" in capsys.readouterr().err
    assert _main("resume", "ffff") == 2
    assert "no session 'ffff'" in capsys.readouterr().err


# -- The sidebar --

@pytest.mark.asyncio
async def test_the_picker_filters_and_enter_resumes_with_the_title(tmp_path, project,
                                                                   _isolated):
    _three(tmp_path, project, _isolated)
    mux = FakeMux()
    app = _sidebar(mux)
    async with app.run_test(size=SIZE) as pilot:
        await _settle(app, pilot)
        app.action_command_line()
        await pilot.pause()
        await _submit(app, pilot, "resume")
        await _settle(app, pilot)
        picker = app.screen
        assert isinstance(picker, ResumePickerScreen)
        assert picker.option_list.option_count == 3
        assert "tidy the docs" in str(picker.option_list.get_option_at_index(0).prompt)
        await pilot.press(*"codex wo")
        await pilot.pause()
        assert [s.session_id for s in picker.shown] == [CODEX_ID]
        await pilot.press("enter")
        await _settle(app, pilot)
        assert not isinstance(app.screen, ResumePickerScreen)
    [record] = launches.list_records()
    assert (record.profile, record.args, record.label) == ("cx", ("resume", CODEX_ID),
                                                           "codex work")
    assert [call["name"] for call in mux.spawned] == ["codex work"]


@pytest.mark.asyncio
async def test_enter_on_a_session_running_here_shows_its_pane(tmp_path, project, _isolated,
                                                              monkeypatch):
    _three(tmp_path, project, _isolated)
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
        app.action_resume_session()
        await _settle(app, pilot)
        picker = app.screen
        assert isinstance(picker, ResumePickerScreen)
        first = str(picker.option_list.get_option_at_index(0).prompt)
        assert first.startswith(RUNNING_MARK) and "claude work" in first
        await pilot.press("enter")
        await _settle(app, pilot)
    assert [session.session_id for session in focused] == [CLAUDE_ID]
    assert not mux.spawned and launches.list_records() == []


@pytest.mark.asyncio
async def test_resume_with_dir_on_the_command_line(tmp_path, project, _isolated):
    _three(tmp_path, project, _isolated)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    mux = FakeMux()
    app = _sidebar(mux)
    async with app.run_test(size=SIZE) as pilot:
        await _settle(app, pilot)
        app.action_command_line()
        await pilot.pause()
        await _submit(app, pilot, f"resume 01a0 {elsewhere}")
        assert app.command_line.result_text == (
            f"resumed Pi session 01a0c4d5 with profile pi in {elsewhere}")
        await _submit(app, pilot, "resume ffff")
        assert "no session 'ffff'" in app.command_line.result_text
    [record] = launches.list_records()
    assert (record.folder, record.label) == (str(elsewhere), "tidy the docs")


@pytest.mark.asyncio
async def test_a_resume_launch_without_a_hook_row_is_found_by_its_pid(project):
    row = _record("codex", "other-session", project, pid="4242",
                  env={"ZELLIJ_SESSION_NAME": SESSION})
    app = _sidebar(FakeMux(), [row])
    async with app.run_test(size=SIZE) as pilot:
        await _settle(app, pilot)
        live = resume.LiveProcess(4242, "in this workspace", True)
        assert app._live_row(CODEX_ID, live).session_id == "other-session"
        assert app._live_row(CODEX_ID, resume.LiveProcess(1, "here", True)) is None
