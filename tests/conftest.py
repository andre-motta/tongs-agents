import json
import pytest

from tongs_agents import paths
from tongs_agents.terminal_apis import _ancestry


@pytest.fixture(autouse=True)
def _private_state_dir(tmp_path, monkeypatch):
    """Never let a test read or write the real tongs-agents state directory."""
    monkeypatch.setenv(paths.STATE_DIR_ENV, str(tmp_path / "state"))


@pytest.fixture(autouse=True)
def _no_real_process_ancestry(monkeypatch):
    """Keep terminal detection independent of the terminal running pytest."""

    def unreadable(pid):
        raise OSError("process ancestry is mocked in tests")

    monkeypatch.setattr(_ancestry, "_read_stat", unreadable)


@pytest.fixture
def fleet_dir():
    return paths.sessions_dir(create=True)


@pytest.fixture
def settings_file(tmp_path, monkeypatch):
    f = tmp_path / "settings.json"
    f.write_text("{}")
    import tongs_agents.cli as cli
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setattr(cli, "SETTINGS_FILE", f)
    monkeypatch.setattr(cli, "CLAUDE_DIR", tmp_path)
    monkeypatch.setattr(cli, "CODEX_DIR", tmp_path / "codex")
    monkeypatch.setattr(cli, "CODEX_HOOKS_FILE", tmp_path / "codex" / "hooks.json")
    monkeypatch.setattr(cli, "CLAUDE_USER_CONFIG", tmp_path / ".claude.json")
    monkeypatch.setattr(cli, "_install_claude_mcp", lambda command: None)
    monkeypatch.setattr(cli, "_uninstall_claude_mcp", lambda command: None)
    monkeypatch.setattr(cli, "_install_codex_mcp", lambda command: None)
    monkeypatch.setattr(cli, "_uninstall_codex_mcp", lambda command: None)
    return f


def write_session(fleet_dir, session_id, repo, cwd, status="started",
                  detail="", pid="", ts=None, source=None, agent=None):
    import time
    if ts is None:
        ts = int(time.time())
    data = {
        "session_id": session_id,
        "repo": repo,
        "cwd": cwd,
        "pid": pid,
        "status": status,
        "detail": detail,
        "ts": ts,
        "started": ts,
    }
    if source:
        data["source"] = source
    if agent:
        data["agent"] = agent
    filename = f"{session_id}.json"
    (fleet_dir / filename).write_text(json.dumps(data))
    return data
