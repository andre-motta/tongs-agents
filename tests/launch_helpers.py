"""Shared launch-record helpers for the launch identity and validation tests."""

import time

from tongs_agents import discovery, launches

LAUNCH_ID = "0123456789abcdef0123456789abcdef"
HOOK_PID = 12345
HOOK_START = "100"


def write_claim(launch_id=LAUNCH_ID, pid=HOOK_PID, start=HOOK_START, *,
                harness="claude", profile="reviewer", state="running"):
    """A launch record for ``launch_id`` claimed by ``pid`` (the launcher)."""
    if start is None and state == "running":
        info = discovery.get_process_info(pid)
        start = info.start_token if info is not None else None
    record = launches.LaunchRecord(
        launch_id=launch_id,
        profile=profile,
        harness=harness,
        multiplexer="zellij",
        session="agents",
        created=int(time.time()),
        pane_id="1" if state == "running" else None,
        pid=pid if state == "running" else None,
        process_start=start if state == "running" else None,
        state=state,
    )
    return launches._write(record)
