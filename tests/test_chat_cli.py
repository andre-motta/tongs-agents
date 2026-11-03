"""``tongs-agents chat`` verbs, with a mocked pane check and process table."""

import argparse
import io
import json

import pytest

from tests.test_chat_grants import world  # noqa: F401
from tests.test_chat_identity import LAUNCH
from tongs_agents.chat import identity
from tongs_agents.chat import store as chat_store
from tongs_agents.chat.store import ChatStore
from tongs_agents.commands import chat


def run(*argv):
    parser = argparse.ArgumentParser()
    chat.add_arguments(parser)
    return chat.run(parser.parse_args(list(argv)))


@pytest.fixture(autouse=True)
def maintainer(monkeypatch):
    monkeypatch.delenv(identity.LAUNCH_ENV, raising=False)
    monkeypatch.setattr(chat, "maintainer_refusal", lambda: None)


@pytest.fixture
def tty(monkeypatch):
    answers = []
    monkeypatch.setattr(chat, "_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: answers.pop(0))
    return answers


def test_every_verb_refuses_inside_an_agent_tree(monkeypatch, capsys):
    monkeypatch.setattr(chat, "maintainer_refusal",
                        lambda: "refusing inside an agent session")
    for argv in (["grants"], ["post", "general", "hi"], ["revoke", LAUNCH], []):
        assert run(*argv) == 5
        assert "refusing inside an agent session" in capsys.readouterr().err


def test_the_real_check_refuses_with_a_launch_id_in_the_env(monkeypatch):
    monkeypatch.setattr(chat, "maintainer_refusal", identity.maintainer_refusal)
    monkeypatch.setenv(identity.LAUNCH_ENV, LAUNCH)
    assert run("grants") == 5


def test_grant_needs_a_terminal_and_has_no_yes(world, monkeypatch, capsys):  # noqa: F811
    world.launch()
    monkeypatch.setattr(chat, "_interactive", lambda: False)
    assert run("grant", LAUNCH) == 5
    assert "interactive terminal" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        run("grant", LAUNCH, "--yes")
    assert not chat_store.exists()


def test_grant_needs_the_typed_handle(world, tty, capsys):  # noqa: F811
    world.launch()
    tty.append("wrong")
    assert run("grant", LAUNCH) == 1
    assert not chat_store.exists()
    tty.append("@ops")
    assert run("grant", LAUNCH, "--wake", "--as", "ops") == 0
    out = capsys.readouterr().out
    assert "pane agents:1" in out and LAUNCH in out and "Granted chat+wake to @ops" in out
    with ChatStore.maintainer() as current:
        grant = current.grant_for(LAUNCH)
    assert (grant.handle, grant.level, grant.granted_via) == ("ops", "chat+wake", "cli")


def test_grants_post_read_channels_and_revoke(world, tty, capsys, monkeypatch):  # noqa: F811
    world.launch()
    tty.append("lead")
    assert run("grant", LAUNCH) == 0
    capsys.readouterr()
    assert run("grants", "--json") == 0
    rows = json.loads(capsys.readouterr().out)
    assert [(row["handle"], row["state"]) for row in rows] == [
        ("lead", "granted, tools pending"),
    ]
    assert run("channel-create", "ops") == 0
    assert run("post", "ops", "hello @lead") == 0
    assert capsys.readouterr().out.splitlines()[-1] == "1 mentioned @lead"
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(b"from stdin\n")))
    assert run("post", "ops", "-", "--thread", "1") == 0
    assert run("read", "ops", "--json") == 0
    capsys.readouterr()
    assert run("read", "ops") == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines == ["1 @maintainer: hello @lead", "2 re 1 @maintainer: from stdin"]
    assert run("channels") == 0
    assert capsys.readouterr().out.splitlines() == ["#general", "#ops"]
    assert run("channel-archive", "ops") == 0
    assert run("post", "ops", "late") == 5
    assert run("post", "nowhere", "late") == 1
    assert run("revoke", LAUNCH) == 0
    assert run("revoke", LAUNCH) == 1
    assert run("grants") == 0
    assert "No chat grants." in capsys.readouterr().out


def test_prune_and_purge(tty, capsys, monkeypatch):
    assert run("prune") == 0 and "No chat file" in capsys.readouterr().out
    ChatStore.maintainer(create=True).close()
    assert run("prune") == 0
    assert "Deleted 0 messages." in capsys.readouterr().out
    tty.append("no")
    assert run("purge") == 1 and chat_store.exists()
    tty.append("purge")
    assert run("purge") == 0 and not chat_store.exists()
    ChatStore.maintainer(create=True).close()
    monkeypatch.setattr(chat, "_interactive", lambda: False)
    assert run("purge") == 5 and chat_store.exists()
    assert run("purge", "--yes") == 0 and not chat_store.exists()


def test_no_verb_runs_the_chat_pane(monkeypatch):
    started = []
    monkeypatch.setattr(chat.ChatApp, "run", lambda app: started.append(app))
    assert run() == 0
    assert isinstance(started[0], chat.ChatApp)


@pytest.mark.parametrize("argv", [["--bogus"], ["post"], ["nosuchverb"]])
def test_the_pane_entry_point_exits_1_on_a_usage_error(argv, capsys):
    with pytest.raises(SystemExit) as raised:
        chat.main(argv)
    assert raised.value.code == 1
    assert "usage: tongs-agents chat" in capsys.readouterr().err
