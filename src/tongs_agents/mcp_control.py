"""Opt-in MCP agent control: spawn, send, status and wait for other agents.

The MCP server registers these tools only when ``[control].mcp_enabled`` is
true, and every call checks it again. The caller is the
``TONGS_AGENTS_LAUNCH_ID`` of the server process: it must name a launch that
``control.verify`` accepts, and the server must run under that launch's
claimed process, so a leaked or copied variable names nobody. On top of
``control.py``'s pane verification each call requires:

- the caller's profile, the profile to spawn and the target launch's profile
  are named profiles with ``mcp_control = true``;
- the target is not the caller's own launch or pane (multiplexer, session,
  pane and socket of the caller's verified launch record, never the server's
  environment);
- the target was spawned by this caller (``spawned_by`` on its launch record),
  unless ``[control].mcp_any_target`` is true;
- at most ``[control].mcp_rate_per_minute`` sends, and as many spawns, per
  caller in any 60 s; a call reserves its slot when it starts and gives it
  back when it is refused or fails, so only a call that succeeded counts;
- spawns go only into the running session the caller's verified launch
  runs in, through that launch's multiplexer and socket; neither the
  server's environment nor ``[workspace]`` picks another one.

No tool takes argv, env or cwd: a spawn names a profile and nothing else.
Actions run through ``control.py`` with actor ``mcp:<caller launch id>``, and
every call, including refusals made here, is audited by ``control.audit``.
A wait ends early when the call is cancelled (the server is shutting down),
checked every poll, or when the caller's launch fails two verifications in a
row; the caller is re-verified about every 5 s, so one transient multiplexer
error does not end the wait.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager

from tongs_agents import app_config, control, discovery, hook, launches, paths
from tongs_agents import profiles as profiles_module
from tongs_agents import workspace_spawn
from tongs_agents.app_config import AppConfig, AppConfigError
from tongs_agents.launches import LaunchError, LaunchRecord
from tongs_agents.multiplexers import SESSION_RUNNING, get_multiplexer
from tongs_agents.multiplexers.base import MultiplexerError, Unsupported
from tongs_agents.profiles import PROFILE_NAME_RE, Profile, ProfileError

ACTOR = "mcp"
RATE_DIR = "mcp-rate"
RATE_WINDOW = 60.0
MAX_RATE_BYTES = 16 * 1024
CLAIM_TIMEOUT = 5.0
CLAIM_POLL = 0.1
VERIFY_INTERVAL = 5.0
VERIFY_FAILURES = 2
SEND = "send"
SPAWN = "spawn"


class RateLimited(control.Refused):
    """The caller used up its sends or spawns for the last minute."""

    outcome = "rate-limited"


class WaitCancelled(control.ControlError):
    """The wait was cancelled, normally because the MCP server is shutting down."""

    outcome = "cancelled"


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


def _recent(stamps: list[float], now: float) -> list[float]:
    return [stamp for stamp in stamps if now - RATE_WINDOW < stamp <= now]


def _release(directory, path, lock, reserved: float) -> None:
    with control._locked(lock):
        stamps = _read_stamps(path)
        if reserved in stamps:
            stamps.remove(reserved)
            _write_stamps(directory, path, stamps)


@contextmanager
def rate_slot(caller: str, action: str, limit: int,
              now: float | None = None) -> Iterator[None]:
    """Run the body as one ``action`` of ``caller``; raise RateLimited past ``limit``.

    The window is kept in ``runtime_dir()/mcp-rate`` so it holds across
    server restarts and concurrent servers of one caller. Under a per-caller
    lock the slot is checked and reserved with a stamp; the lock is released
    while the body runs, so parallel calls of one caller do not queue behind
    each other. A body that raises (a refused or failed call) gives its
    reservation back, so it does not count.
    """
    try:
        directory, path = _rate_path(caller, action)
        lock = directory / f".{caller}.lock"
        with control._locked(lock):
            reserved = time.time() if now is None else now
            stamps = _recent(_read_stamps(path), reserved)
            if len(stamps) >= limit:
                raise RateLimited(
                    f"rate limit: at most {limit} {action}s per minute per caller "
                    "([control].mcp_rate_per_minute)"
                )
            _write_stamps(directory, path, [*stamps, reserved])
    except OSError as error:
        raise control.ControlError(
            f"cannot use the MCP rate limit file: {error}"
        ) from None
    try:
        yield
    except BaseException:
        try:
            _release(directory, path, lock, reserved)
        except (OSError, control.ControlError) as error:
            print(f"tongs-agents: MCP rate limit not released: {error}", file=sys.stderr)
        raise


class McpControl:
    """The policy for one MCP server process; ``env`` is that process's environment."""

    def __init__(self, env: Mapping[str, str] | None = None, *, pid: int | None = None,
                 claim_timeout: float = CLAIM_TIMEOUT,
                 verify_interval: float = VERIFY_INTERVAL):
        self.env = dict(os.environ if env is None else env)
        self.pid = os.getpid() if pid is None else pid
        self.claim_timeout = claim_timeout
        self.verify_interval = verify_interval
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
            and control._same_dir(record.socket_dir, caller.socket_dir)
        ):
            raise control.Refused(f"launch {launch_id} names the caller's own pane")
        self._opted(loaded, record.profile, "target")
        if not config.control.mcp_any_target and record.spawned_by != caller.launch_id:
            raise control.Refused(
                f"launch {launch_id} was not spawned by this caller "
                "([control].mcp_any_target = false)"
            )
        return record

    def _checked(self, launch_id) -> tuple[AppConfig, dict[str, Profile], LaunchRecord]:
        config = self._config()
        loaded = self._profiles()
        caller = self._caller(loaded)
        self._target(config, loaded, caller, launch_id)
        return config, loaded, caller

    # Tools

    def send(self, launch_id: str, text: str) -> dict:
        """Paste ``text`` into a target pane and press Enter, through control.send."""
        data = text.encode("utf-8", errors="replace") if isinstance(text, str) else None
        sending = False
        try:
            config, _, caller = self._checked(launch_id)
            settings = control.Settings(
                send_delay_ms=config.control.send_delay_ms,
                mcp_enabled=config.control.mcp_enabled,
            )
            with rate_slot(caller.launch_id, SEND, config.control.mcp_rate_per_minute):
                sending = True
                control.send(launch_id, text, actor=self.actor, caller=self.caller,
                             settings=settings)
        except control.ControlError as error:
            if not sending:
                self._audit(SEND, launch_id, error, data)
            raise
        return {"ok": True, "launch_id": launch_id, "bytes": len(data or b"")}

    def status(self, launch_id: str) -> dict:
        try:
            self._checked(launch_id)
        except control.ControlError as error:
            self._audit("status", launch_id, error)
            raise
        result = control.status(launch_id, actor=self.actor, caller=self.caller)
        return {"ok": True, "status": result.to_json()}

    def wait(self, launch_id: str, until: str, timeout_s: float,
             stop: threading.Event | None = None) -> dict:
        """control.wait, ended early once ``stop`` is set (checked every poll)
        or the caller fails ``VERIFY_FAILURES`` verifications in a row
        (re-verified about every ``verify_interval`` seconds).
        """
        try:
            _, loaded, _ = self._checked(launch_id)
        except control.ControlError as error:
            self._audit("wait", launch_id, error)
            raise
        verified = time.monotonic()
        failures = 0

        def still_waiting() -> None:
            nonlocal verified, failures
            if stop is not None and stop.is_set():
                raise WaitCancelled(
                    "wait cancelled: the call was cancelled or the MCP server is "
                    "shutting down"
                )
            if time.monotonic() - verified < self.verify_interval:
                return
            verified = time.monotonic()
            try:
                self._caller(loaded)
            except control.ControlError:
                failures += 1
                if failures >= VERIFY_FAILURES:
                    raise
                return
            failures = 0

        result = control.wait(launch_id, until, timeout_s, actor=self.actor,
                              caller=self.caller, check=still_waiting)
        return {
            "ok": True,
            "reached": result.reached,
            "timed_out": result.timed_out,
            "exit_code": result.exit_code,
            "status": result.status.to_json(),
        }

    @staticmethod
    def _session(caller: LaunchRecord) -> tuple[str, str, str | None]:
        """The multiplexer, session and socket of the caller's verified launch."""
        session = caller.session
        if not app_config.SESSION_RE.match(session):
            raise control.Refused(f"workspace session {session!r:.80} is not a valid name")
        return caller.multiplexer, session, caller.socket_dir

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
        """Start ``profile_name`` in a new pane of the running workspace session.

        Placement notices (a Zellij agent area that lost its home pane) are
        returned under ``notices``.
        """
        record = None
        notices: list[str] = []
        try:
            config = self._config()
            loaded = self._profiles()
            caller = self._caller(loaded)
            if caller.spawned_by is not None and not config.control.mcp_nested_spawn:
                # An agent started through MCP may not start more agents by
                # default, so one instructed lead cannot grow a tree of panes.
                raise control.Refused(
                    "this agent was itself started through MCP; nested spawns need "
                    "[control].mcp_nested_spawn = true"
                )
            profile = self._opted(loaded, profile_name, "spawn")
            multiplexer, session, socket_dir = self._session(caller)
            try:
                mux = get_multiplexer(multiplexer, socket_dir=socket_dir)
                state = mux.session_state(session)
            except Unsupported as error:
                raise control.ControlUnsupported(error.detail) from None
            except MultiplexerError as error:
                raise control.Refused(
                    f"cannot check workspace session {session!r}: {error.detail}"
                ) from None
            if state != SESSION_RUNNING:
                raise control.Refused(f"workspace session {session!r} is not running")
            with rate_slot(caller.launch_id, SPAWN, config.control.mcp_rate_per_minute):
                try:
                    record = launches.create_pending(
                        profile, session, mux.name, spawned_by=caller.launch_id
                    )
                    ref = workspace_spawn.spawn_launch(mux, record, notices=notices)
                except (MultiplexerError, LaunchError, OSError) as error:
                    if record is not None:
                        workspace_spawn.discard_launches([record])
                    detail = getattr(error, "detail", None) or str(error)
                    raise control.ControlError(
                        f"spawn of {profile.name} failed: {detail}"
                    ) from None
        except control.ControlError as error:
            self._audit(SPAWN, None, error)
            raise
        control.audit(SPAWN, actor=self.actor, launch_id=record.launch_id, outcome="ok",
                      exit_code=control.EXIT_OK, caller=self.caller)
        claimed = self._await_claim(record.launch_id)
        result = {
            "ok": True,
            "launch_id": record.launch_id,
            "profile": record.profile,
            "session": session,
            "pane_id": (claimed.pane_id if claimed and claimed.pane_id else ref.pane_id),
            "state": claimed.state if claimed is not None else "unknown",
        }
        if notices:
            result["notices"] = notices
        return result
