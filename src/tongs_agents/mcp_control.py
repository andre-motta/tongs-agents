"""Opt-in MCP agent control: spawn, send, status and wait for other agents.

The MCP server registers these tools only when ``[control].mcp_enabled`` is
true, and every call checks it again. The caller is the
``TONGS_AGENTS_LAUNCH_ID`` of the server process: it must name a launch that
``control.verify`` accepts, and the server must run under that launch's
claimed process, so a leaked or copied variable names nobody. On top of
``control.py``'s pane verification each call requires:

- the caller's profile, the profile to spawn and the target launch's profile
  are named profiles with ``mcp_control = true``;
- the target is not the caller's own launch or pane;
- the target was spawned by this caller (``spawned_by`` on its launch record),
  unless ``[control].mcp_any_target`` is true;
- at most ``[control].mcp_rate_per_minute`` sends, and as many spawns, per
  caller in any 60 s;
- spawns go only into a running workspace session: the server's
  ``ZELLIJ_SESSION_NAME``, else ``[workspace].session``.

No tool takes argv, env or cwd: a spawn names a profile and nothing else.
Actions run through ``control.py`` with actor ``mcp:<caller launch id>``, and
every call, including refusals made here, is audited by ``control.audit``.
"""

from __future__ import annotations

import json
import os
import stat
import time
from collections.abc import Mapping

from tongs_agents import app_config, control, discovery, hook, launches, paths
from tongs_agents import profiles as profiles_module
from tongs_agents import workspace_spawn
from tongs_agents.app_config import AppConfig, AppConfigError
from tongs_agents.launches import LaunchError, LaunchRecord
from tongs_agents.multiplexers import SESSION_RUNNING, get_multiplexer
from tongs_agents.multiplexers.base import MultiplexerError, Unsupported
from tongs_agents.profiles import PROFILE_NAME_RE, Profile, ProfileError

ACTOR = "mcp"
SESSION_ENV = "ZELLIJ_SESSION_NAME"
SOCKET_DIR_ENV = "ZELLIJ_SOCKET_DIR"
RATE_DIR = "mcp-rate"
RATE_WINDOW = 60.0
MAX_RATE_BYTES = 16 * 1024
CLAIM_TIMEOUT = 5.0
CLAIM_POLL = 0.1
SEND = "send"
SPAWN = "spawn"


class RateLimited(control.Refused):
    """The caller used up its sends or spawns for the last minute."""

    outcome = "rate-limited"


def error_payload(error: control.ControlError) -> dict:
    """The JSON object a tool returns for a refused or failed call."""
    return {
        "ok": False,
        "outcome": error.outcome,
        "exit_code": error.exit_code,
        "error": error.detail,
    }


def _rate_path(caller: str, action: str):
    directory = paths.ensure_private_dir(paths.runtime_dir(create=True) / RATE_DIR)
    return directory, directory / f"{caller}.{action}.json"


def _read_stamps(path) -> list[float]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return []
    try:
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return []
            data = json.loads(stream.read(MAX_RATE_BYTES))
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [
        float(item) for item in data
        if isinstance(item, (int, float)) and not isinstance(item, bool)
    ]


def _write_stamps(directory, path, stamps: list[float]) -> None:
    temp = directory / f".{path.name}.{os.getpid()}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temp, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(stamps, stream)
    os.replace(temp, path)


def take_rate(caller: str, action: str, limit: int, now: float | None = None) -> None:
    """Count one ``action`` for ``caller``; raise RateLimited past ``limit`` a minute.

    The window is kept in ``runtime_dir()/mcp-rate`` under a lock, so it holds
    across server restarts and concurrent servers of one caller. A refused
    call is not counted.
    """
    try:
        directory, path = _rate_path(caller, action)
        with control._locked(directory / f".{caller}.lock"):
            now = time.time() if now is None else now
            stamps = [
                stamp for stamp in _read_stamps(path)
                if now - RATE_WINDOW < stamp <= now
            ]
            if len(stamps) >= limit:
                raise RateLimited(
                    f"rate limit: at most {limit} {action}s per minute per caller "
                    "([control].mcp_rate_per_minute)"
                )
            stamps.append(now)
            _write_stamps(directory, path, stamps)
    except OSError as error:
        raise control.ControlError(f"cannot use the MCP rate limit file: {error}") from None


class McpControl:
    """The policy for one MCP server process; ``env`` is that process's environment."""

    def __init__(self, env: Mapping[str, str] | None = None, *, pid: int | None = None,
                 claim_timeout: float = CLAIM_TIMEOUT):
        self.env = dict(os.environ if env is None else env)
        self.pid = os.getpid() if pid is None else pid
        self.claim_timeout = claim_timeout
        self.caller = control.caller_launch_id(self.env)
        self.actor = f"{ACTOR}:{self.caller}" if self.caller else ACTOR

    # Shared checks

    def _audit(self, action: str, launch_id, error: control.ControlError,
               data: bytes | None = None) -> None:
        control.audit(action, actor=self.actor, launch_id=launch_id,
                      outcome=error.outcome, exit_code=error.exit_code,
                      data=data, caller=self.caller)

    def _config(self) -> AppConfig:
        try:
            config = app_config.load_app_config()
        except AppConfigError as error:
            raise control.ControlError(str(error)) from None
        if not config.control.mcp_enabled:
            raise control.Refused("MCP control is off ([control].mcp_enabled = false)")
        return config

    def _profiles(self) -> dict[str, Profile]:
        try:
            return profiles_module.load_profiles()
        except ProfileError as error:
            raise control.ControlError(str(error)) from None

    @staticmethod
    def _opted(loaded: dict[str, Profile], name, role: str) -> Profile:
        if not isinstance(name, str) or not PROFILE_NAME_RE.match(name):
            raise control.Refused(f"{role} profile: not a profile name")
        profile = loaded.get(name)
        if profile is None:
            raise control.Refused(f"{role} profile {name!r} is not a named profile")
        if not profile.mcp_control:
            raise control.Refused(
                f"{role} profile {name!r} has not opted in to MCP control "
                "(mcp_control = true)"
            )
        return profile

    def _runs_under(self, record: LaunchRecord) -> bool:
        process = discovery.get_process_info(self.pid)
        return hook.process_descends_from(process, record.pid, record.process_start)

    def _caller(self, loaded: dict[str, Profile]) -> LaunchRecord:
        if self.caller is None:
            raise control.Refused(
                "this MCP server does not run in a pane tongs-agents launched "
                f"(no valid {control.CALLER_ENV})"
            )
        try:
            control.verify(self.caller)
        except control.ControlError as error:
            raise control.Refused(f"caller: {error.detail}") from None
        record = launches.get(self.caller)
        if record is None or not self._runs_under(record):
            raise control.Refused(
                f"this MCP server does not run under the process of caller launch "
                f"{self.caller}"
            )
        self._opted(loaded, record.profile, "caller")
        return record

    def _target(self, config: AppConfig, loaded: dict[str, Profile],
                caller: LaunchRecord, launch_id) -> LaunchRecord:
        if not launches.is_launch_id(launch_id):
            raise control.Refused("not a tongs-agents launch id")
        if launch_id == caller.launch_id:
            raise control.Refused("refusing to act on the caller's own launch")
        record = launches.get(launch_id)
        if record is None:
            raise control.Refused(f"launch {launch_id} was not spawned by tongs-agents")
        if (
            record.pane_id is not None
            and (record.multiplexer, record.session, record.pane_id)
            == (caller.multiplexer, caller.session, caller.pane_id)
        ):
            raise control.Refused(f"launch {launch_id} names the caller's own pane")
        self._opted(loaded, record.profile, "target")
        if not config.control.mcp_any_target and record.spawned_by != caller.launch_id:
            raise control.Refused(
                f"launch {launch_id} was not spawned by this caller "
                "([control].mcp_any_target = false)"
            )
        return record

    def _checked(self, launch_id) -> tuple[AppConfig, LaunchRecord]:
        config = self._config()
        loaded = self._profiles()
        caller = self._caller(loaded)
        self._target(config, loaded, caller, launch_id)
        return config, caller

    # Tools

    def send(self, launch_id: str, text: str) -> dict:
        """Paste ``text`` into a target pane and press Enter, through control.send."""
        data = text.encode("utf-8", errors="replace") if isinstance(text, str) else None
        try:
            config, caller = self._checked(launch_id)
            take_rate(caller.launch_id, SEND, config.control.mcp_rate_per_minute)
        except control.ControlError as error:
            self._audit(SEND, launch_id, error, data)
            raise
        settings = control.Settings(
            send_delay_ms=config.control.send_delay_ms,
            mcp_enabled=config.control.mcp_enabled,
        )
        control.send(launch_id, text, actor=self.actor, caller=self.caller,
                     settings=settings)
        return {"ok": True, "launch_id": launch_id, "bytes": len(data or b"")}

    def status(self, launch_id: str) -> dict:
        try:
            self._checked(launch_id)
        except control.ControlError as error:
            self._audit("status", launch_id, error)
            raise
        result = control.status(launch_id, actor=self.actor, caller=self.caller)
        return {"ok": True, "status": result.to_json()}

    def wait(self, launch_id: str, until: str, timeout_s: float) -> dict:
        try:
            self._checked(launch_id)
        except control.ControlError as error:
            self._audit("wait", launch_id, error)
            raise
        result = control.wait(launch_id, until, timeout_s, actor=self.actor,
                              caller=self.caller)
        return {
            "ok": True,
            "reached": result.reached,
            "timed_out": result.timed_out,
            "exit_code": result.exit_code,
            "status": result.status.to_json(),
        }

    def _session(self, config: AppConfig) -> tuple[str, str | None]:
        session = self.env.get(SESSION_ENV)
        if session:
            socket_dir = self.env.get(SOCKET_DIR_ENV) or None
            if socket_dir is not None and not os.path.isabs(socket_dir):
                socket_dir = None
        else:
            session, socket_dir = config.workspace.session, None
        if not app_config.SESSION_RE.match(session):
            raise control.Refused(f"workspace session {session!r:.80} is not a valid name")
        return session, socket_dir

    def _await_claim(self, launch_id: str) -> LaunchRecord | None:
        deadline = time.monotonic() + self.claim_timeout
        while True:
            record = launches.get(launch_id)
            if record is None or record.state == "running":
                return record
            if time.monotonic() >= deadline:
                return record
            time.sleep(CLAIM_POLL)

    def spawn(self, profile_name: str) -> dict:
        """Start ``profile_name`` in a new pane of the running workspace session."""
        record = None
        try:
            config = self._config()
            loaded = self._profiles()
            caller = self._caller(loaded)
            profile = self._opted(loaded, profile_name, "spawn")
            session, socket_dir = self._session(config)
            try:
                mux = get_multiplexer(config.workspace.multiplexer, socket_dir=socket_dir)
                state = mux.session_state(session)
            except Unsupported as error:
                raise control.ControlUnsupported(error.detail) from None
            except MultiplexerError as error:
                raise control.Refused(
                    f"cannot check workspace session {session!r}: {error.detail}"
                ) from None
            if state != SESSION_RUNNING:
                raise control.Refused(f"workspace session {session!r} is not running")
            take_rate(caller.launch_id, SPAWN, config.control.mcp_rate_per_minute)
            try:
                record = launches.create_pending(
                    profile, session, mux.name, spawned_by=caller.launch_id
                )
                ref = workspace_spawn.spawn_launch(mux, record)
            except (MultiplexerError, LaunchError, OSError) as error:
                if record is not None:
                    workspace_spawn.discard_launches([record])
                detail = getattr(error, "detail", None) or str(error)
                raise control.ControlError(f"spawn of {profile.name} failed: {detail}") from None
        except control.ControlError as error:
            self._audit(SPAWN, None, error)
            raise
        control.audit(SPAWN, actor=self.actor, launch_id=record.launch_id, outcome="ok",
                      exit_code=control.EXIT_OK, caller=self.caller)
        claimed = self._await_claim(record.launch_id)
        return {
            "ok": True,
            "launch_id": record.launch_id,
            "profile": record.profile,
            "session": session,
            "pane_id": (claimed.pane_id if claimed and claimed.pane_id else ref.pane_id),
            "state": claimed.state if claimed is not None else "unknown",
        }
