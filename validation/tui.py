"""Exercise the real Textual app with synthetic temporary session records.

The child process runs with a temporary HOME and XDG directories, so the
records go through discovery's public writer into a throwaway session store
whatever its path is. Only the live process scan is stubbed.
"""

import asyncio
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from textual.geometry import Region

from tongs_agents import discovery
from tongs_agents.models import UNRESOLVED_INSTANCE_ID, SessionStatus, canonical_session_id
from tongs_agents.tui import FleetMonitorApp
from tongs_agents.widgets.agents_panel import AgentsPanel
from tongs_agents.widgets.detail_panel import DetailPanel
from tongs_agents.widgets.session_table import SessionTable, compact_status_label

ISOLATION_MARKER = "TONGS_AGENTS_TUI_VALIDATION_HOME"
FIXTURES = (
    ("claude", SessionStatus.RUNNING, "svc-run"),
    ("codex", SessionStatus.IDLE, "svc-idle"),
    ("pi", SessionStatus.WAITING, "svc-wait"),
    ("claude", SessionStatus.STARTED, "svc-new"),
    ("codex", SessionStatus.ERROR, "svc-err"),
    ("pi", SessionStatus.ENDED, "svc-end"),
    ("claude", SessionStatus.DISCOVERED, "svc-scan"),
)


def _write_fixtures(cwd: Path) -> None:
    now = int(time.time())
    for index, (agent, status, repo) in enumerate(FIXTURES):
        session_id = f"synthetic-{index}"
        written = discovery.write_session_record({
            "schema_version": 1,
            "session_id": session_id,
            "harness_id": agent,
            "agent": agent,
            "instance_id": UNRESOLVED_INSTANCE_ID,
            "canonical_id": canonical_session_id(agent, session_id, UNRESOLVED_INSTANCE_ID),
            "process_identity": "unresolved",
            "repo": repo,
            "cwd": str(cwd),
            "pid": "",
            "status": status.value,
            "detail": "synthetic UI fixture",
            "ts": now,
            "started": now,
            "focus_target": {"kind": "unavailable"},
        })
        assert written, repo


def _table_lines(table: SessionTable) -> list[str]:
    region = Region(0, 0, table.size.width, table.size.height)
    return [strip.text for strip in table.render_lines(region)]


def _assert_all_statuses_render(table: SessionTable) -> None:
    lines = _table_lines(table)
    for _, status, repo in FIXTURES:
        label = compact_status_label(status) if table.compact else status.value.upper()
        row = next((line for line in lines if re.search(rf"\b{re.escape(repo)}\b", line)), "")
        assert label in row, (repo, label, lines)


async def _settle(app, pilot) -> None:
    await app.workers.wait_for_complete()
    await pilot.pause()
    await pilot.pause()


async def validate_full_width() -> None:
    app = FleetMonitorApp(refresh_interval=60, notify_level="none")
    async with app.run_test(size=(120, 35)) as pilot:
        await _settle(app, pilot)
        table = app.query_one(SessionTable)
        assert not table.compact
        assert table.row_count == len(FIXTURES)
        _assert_all_statuses_render(table)
        await pilot.press("3")
        assert table.row_count == 1
        assert app._get_selected_session().agent == "pi"
        await pilot.press("d")
        panel = app.query_one(DetailPanel)
        assert panel.has_class("visible")
        rendered = str(panel.render())
        assert all(value in rendered for value in (
            "pi", "svc-wait", "WAITING", "synthetic UI fixture",
        ))
        await pilot.press("0", "slash")
        search = app.query_one("#search-input")
        search.value = "svc-idle"
        await pilot.pause()
        assert table.row_count == 1
        assert app._get_selected_session().agent == "codex"
        await pilot.press("escape")
        assert table.row_count == len(FIXTURES)
        await pilot.press("n")
        assert app._get_selected_session().repo == "svc-wait"
        await pilot.press("q")
        assert not app.is_running


async def validate_sidebar_width() -> None:
    app = FleetMonitorApp(refresh_interval=60, notify_level="none")
    async with app.run_test(size=(40, 30)) as pilot:
        await _settle(app, pilot)
        table = app.query_one(SessionTable)
        assert table.compact
        assert table.virtual_size.width <= table.size.width
        _assert_all_statuses_render(table)
        summary = str(app.query_one(AgentsPanel).summary_bar.render())
        assert summary.startswith(f"{len(FIXTURES)} agents"), summary
        await pilot.press("q")


async def validate() -> None:
    home = Path.home()
    assert str(home) == os.environ.get(ISOLATION_MARKER), "HOME is not isolated"
    with tempfile.TemporaryDirectory(dir=home) as cwd, patch.object(
        discovery, "_find_agent_processes", return_value=[]
    ):
        _write_fixtures(Path(cwd))
        await validate_full_width()
        await validate_sidebar_width()
    print(
        "PASS: headless Textual app load at 120 and 40 columns, every status,"
        " status/search filters, details, attention and quit"
    )


def _isolated_env(root: Path) -> dict[str, str]:
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith(("FLEET_", "TONGS_AGENTS_", "XDG_"))
    }
    home = root / "home"
    for name in ("config", "state", "data", "cache", "runtime"):
        (root / name).mkdir(mode=0o700)
    home.mkdir(mode=0o700)
    env.update({
        "HOME": str(home),
        "USERPROFILE": str(home),
        "XDG_CONFIG_HOME": str(root / "config"),
        "XDG_STATE_HOME": str(root / "state"),
        "XDG_DATA_HOME": str(root / "data"),
        "XDG_CACHE_HOME": str(root / "cache"),
        "XDG_RUNTIME_DIR": str(root / "runtime"),
        ISOLATION_MARKER: str(home),
    })
    return env


if __name__ == "__main__":
    if sys.argv[1:] == ["--inside"]:
        asyncio.run(validate())
    else:
        with tempfile.TemporaryDirectory(prefix="tongs-agents-tui-validation-") as root:
            try:
                result = subprocess.run(
                    [sys.executable, str(Path(__file__).resolve()), "--inside"],
                    env=_isolated_env(Path(root)),
                    timeout=60,
                )
            except subprocess.TimeoutExpired:
                print("TUI validation timed out after 60 seconds", file=sys.stderr)
                raise SystemExit(124)
        raise SystemExit(result.returncode)
