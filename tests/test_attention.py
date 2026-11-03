"""Attention contract: six hook statuses plus discovered map onto five states."""

import dataclasses

import pytest

from tongs_agents.attention import Attention, attention_state
from tongs_agents.models import FleetSession, SessionStatus


def _session(status):
    return FleetSession(
        session_id="s", repo="r", cwd="/tmp/r", status=status, detail="",
        ts=0, started=0, pid="", terminal="", terminal_env={}, source="hook",
        tool="",
    )


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (SessionStatus.WAITING, Attention.NEEDS_YOU),
        (SessionStatus.ERROR, Attention.FAILED),
        (SessionStatus.STARTED, Attention.WORKING),
        (SessionStatus.RUNNING, Attention.WORKING),
        (SessionStatus.IDLE, Attention.DONE),
        (SessionStatus.ENDED, Attention.QUIET),
        (SessionStatus.DISCOVERED, Attention.QUIET),
    ],
)
def test_attention_state_maps_every_status(status, expected):
    assert attention_state(_session(status)) is expected


def test_every_status_has_an_attention_state():
    assert {attention_state(_session(status)) for status in SessionStatus} == set(Attention)


def test_attention_ignores_needs_attention_flag():
    session = dataclasses.replace(
        _session(SessionStatus.IDLE), needs_attention=True, age_seconds=999
    )
    assert attention_state(session) is Attention.DONE


def test_attention_values_are_stable():
    assert [a.value for a in Attention] == [
        "needs-you", "working", "done", "failed", "quiet",
    ]
