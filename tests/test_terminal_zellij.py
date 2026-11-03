"""The terminal-API Zellij CLI runs the binary the multiplexer layer resolves."""

import subprocess

import pytest

from tongs_agents.multiplexers import zellij_binary
from tongs_agents.terminal_apis import zellij

MANAGED = "/state/bin/zellij"
ON_PATH = "/usr/bin/zellij"


@pytest.fixture
def resolved(monkeypatch):
    def resolve(found, managed):
        monkeypatch.setattr(zellij_binary, "path_binary", lambda: found)
        monkeypatch.setattr(zellij_binary, "managed_binary", lambda: managed)
        monkeypatch.setattr(zellij_binary, "web_binary", lambda: None)
        monkeypatch.setattr(zellij_binary, "_cached_version", lambda binary: (0, 45, 1))

    return resolve


@pytest.mark.parametrize(
    ("found", "managed", "expected"),
    [
        (None, MANAGED, MANAGED),
        (ON_PATH, None, "zellij"),
        (ON_PATH, MANAGED, "zellij"),
        (None, None, "zellij"),
    ],
    ids=["managed-only", "path-only", "path-at-floor-wins", "none"],
)
def test_binary_follows_the_multiplexer_layer(resolved, found, managed, expected):
    resolved(found, managed)
    assert zellij._binary() == expected


def test_focus_selection_runs_the_managed_build_off_path(resolved, monkeypatch):
    resolved(None, MANAGED)
    calls = []
    shown = iter(["terminal_1", "terminal_3"])

    def run(argv, **kwargs):
        calls.append(argv)
        stdout = ""
        if argv[-1] == "list-clients":
            stdout = f"CLIENT_ID ZELLIJ_PANE_ID RUNNING_COMMAND\n1 {next(shown)} bash\n"
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    monkeypatch.setattr(zellij.subprocess, "run", run)
    monkeypatch.setattr(zellij.time, "sleep", lambda seconds: None)
    api = zellij.ZellijAPI()
    env = {"ZELLIJ": "0", "ZELLIJ_SESSION_NAME": "work", "ZELLIJ_PANE_ID": "3"}
    assert api.switch_tab("3", env) is True
    assert calls == [
        [MANAGED, "--session", "work", "action", "list-clients"],
        [MANAGED, "--session", "work", "action", "focus-pane-id", "terminal_3"],
        [MANAGED, "--session", "work", "action", "list-clients"],
    ]
