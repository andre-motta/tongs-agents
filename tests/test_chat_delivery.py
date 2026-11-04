"""The hook's replacement check (chat.delivery._replaced, #105).

Launch records are real files in the private test state dir. Only records
whose raw bytes could name the launch id are parsed.
"""

import json
import os
import sys

import pytest

from tests.test_chat_identity import LAUNCH, record
from tongs_agents import hook, paths
from tongs_agents.chat import delivery

OTHER = "f" * 32


@pytest.fixture
def parsed(monkeypatch):
    seen = []
    read = hook.read_launch_data

    def spy(launch_id):
        seen.append(launch_id)
        return read(launch_id)

    monkeypatch.setattr(hook, "read_launch_data", spy)
    return seen


def test_unrelated_records_are_never_parsed(parsed):
    record()
    for index in range(20):
        record(f"{index:032x}", replaces=f"{index + 100:032x}")
    assert delivery._replaced(LAUNCH) is False
    assert parsed == []


def test_a_replacing_record_is_found(parsed):
    record()
    record(f"{1:032x}")
    record(OTHER, replaces=LAUNCH, state="pending")
    assert delivery._replaced(LAUNCH) is True
    assert parsed == [OTHER]


def test_a_record_that_only_mentions_the_id_does_not_replace_it(parsed):
    record()
    data = record(OTHER).to_json()
    data["spawned_by"] = LAUNCH
    path = paths.launches_dir() / f"{OTHER}.json"
    path.write_text(json.dumps(data))
    assert delivery._replaced(LAUNCH) is False
    assert parsed == [OTHER]


def test_a_json_escaped_replaces_still_counts(parsed):
    record()
    path = paths.launches_dir() / f"{OTHER}.json"
    record(OTHER)
    data = json.loads(path.read_bytes())
    data["replaces"] = LAUNCH
    text = json.dumps(data).replace(LAUNCH, LAUNCH[:-1] + "\\u0066")
    assert LAUNCH not in text
    path.write_text(text)
    assert delivery._replaced(LAUNCH) is True


def test_other_escapes_do_not_force_a_full_parse(parsed):
    record()
    path = paths.launches_dir() / f"{OTHER}.json"
    record(OTHER)
    data = json.loads(path.read_bytes())
    data["folder"] = "/home/user/caf\u00e9 \"quoted\""
    path.write_text(json.dumps(data))
    assert "\\u00e9" in path.read_text()
    assert delivery._replaced(LAUNCH) is False
    assert parsed == []


def test_a_utf16_record_is_still_read(parsed):
    record()
    path = paths.launches_dir() / f"{OTHER}.json"
    record(OTHER)
    data = json.loads(path.read_bytes())
    data["replaces"] = LAUNCH
    path.write_bytes(json.dumps(data).encode("utf-16-le"))
    assert delivery._replaced(LAUNCH) is True


def test_other_names_and_unreadable_entries_are_skipped(parsed):
    record()
    directory = paths.launches_dir()
    (directory / f".{LAUNCH}.claim").write_text(LAUNCH)
    (directory / "notes.json").write_text(json.dumps({"replaces": LAUNCH}))
    (directory / f"{OTHER}.json").mkdir()
    assert delivery._replaced(LAUNCH) is False
    assert parsed == []


@pytest.mark.skipif(not hasattr(os, "mkfifo") or sys.platform == "win32", reason="needs FIFOs")
def test_a_fifo_at_a_record_path_never_hangs(parsed):
    record()
    os.mkfifo(paths.launches_dir() / f"{OTHER}.json")
    assert delivery._replaced(LAUNCH) is False


def test_a_symlinked_record_is_not_followed(parsed, tmp_path):
    record()
    target = tmp_path / "planted.json"
    target.write_text(json.dumps({"replaces": LAUNCH}))
    (paths.launches_dir() / f"{OTHER}.json").symlink_to(target)
    assert delivery._replaced(LAUNCH) is False
    assert parsed == []


def test_a_missing_launches_dir_replaces_nothing():
    assert delivery._replaced(LAUNCH) is False
