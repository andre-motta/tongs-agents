"""Sandbox launch records: what the bridge needs to find and import one pane's spool.

The launcher writes one record per sandboxed launch in
``state_dir()/sandbox/<launch_id>.json`` before it execs the pane command:
the sandbox name, gateway, mode, working directory and spool inside the
sandbox, the host cwd, and the terminal identity captured in the pane on the
host (row 9). Records are written atomically at 0600 in a private directory
and read without following symlinks, 64 KiB at most. The bridge removes a
record once its launch record is gone.
"""

from __future__ import annotations

import json
import os
import posixpath
import stat
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from tongs_agents import paths
from tongs_agents.launches import is_launch_id
from tongs_agents.profiles import (
    HARNESSES,
    PROFILE_NAME_RE,
    SANDBOX_MODES,
    SANDBOX_NAME_RE,
    SANDBOX_TOKEN_RE,
)

SCHEMA_VERSION = 1
DIRECTORY_NAME = "sandbox"
MAX_RECORD_BYTES = 64 * 1024
MAX_TEXT = 1024
MAX_ENV_ITEMS = 32


class StateError(OSError):
    """The sandbox state directory or a record cannot be used."""


@dataclass(frozen=True)
class SandboxLaunch:
    launch_id: str
    profile: str
    harness: str
    sandbox: str
    mode: str
    workdir: str
    spool: str
    created: int
    gateway: str | None = None
    cwd: str = ""
    terminal: str = ""
    terminal_env: dict = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION

    def to_json(self) -> dict:
        return asdict(self)


def state_root(create: bool = False) -> Path:
    directory = paths.state_dir(create=create) / DIRECTORY_NAME
    if create:
        paths.ensure_private_dir(directory)
    return directory


def _text(value, *, empty: bool = False) -> bool:
    if not isinstance(value, str):
        return False
    if not value:
        return empty
    return len(value) <= MAX_TEXT and value.isprintable()


def _sandbox_path(value) -> bool:
    return (
        _text(value) and value.startswith("/")
        and posixpath.normpath(value) == value and value != "/"
    )


def _env(value) -> bool:
    return (
        isinstance(value, dict) and len(value) <= MAX_ENV_ITEMS
        and all(_text(key) and _text(item, empty=True) for key, item in value.items())
    )


def parse(data) -> SandboxLaunch | None:
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        return None
    gateway = data.get("gateway")
    created = data.get("created")
    workdir = data.get("workdir")
    spool = data.get("spool")
    if not (
        is_launch_id(data.get("launch_id"))
        and isinstance(data.get("profile"), str) and PROFILE_NAME_RE.match(data["profile"])
        and data.get("harness") in HARNESSES
        and isinstance(data.get("sandbox"), str) and SANDBOX_NAME_RE.match(data["sandbox"])
        and data.get("mode") in SANDBOX_MODES
        and _sandbox_path(workdir) and _sandbox_path(spool)
        and spool.startswith(workdir + "/")
        and (gateway is None or (isinstance(gateway, str) and SANDBOX_TOKEN_RE.match(gateway)))
        and _text(data.get("cwd", ""), empty=True)
        and _text(data.get("terminal", ""), empty=True) and len(data.get("terminal", "")) <= 64
        and _env(data.get("terminal_env", {}))
        and type(created) is int
    ):
        return None
    fields = SandboxLaunch.__dataclass_fields__
    values = {key: data[key] for key in fields if key in data}
    values["terminal_env"] = dict(values.get("terminal_env", {}))
    return SandboxLaunch(**values)


def _path(launch_id: str) -> Path:
    if not is_launch_id(launch_id):
        raise StateError(f"invalid launch id {launch_id!r}")
    return state_root() / f"{launch_id}.json"


def _read(path: Path) -> SandboxLaunch | None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            return None
        if os.name != "nt" and info.st_uid != os.getuid():
            return None
        raw = stream.read(MAX_RECORD_BYTES + 1)
    if len(raw) > MAX_RECORD_BYTES:
        return None
    try:
        record = parse(json.loads(raw))
    except (ValueError, UnicodeError, RecursionError):
        return None
    if record is None or path.name != f"{record.launch_id}.json":
        return None
    return record


def write(record: SandboxLaunch) -> SandboxLaunch:
    if parse(record.to_json()) is None:
        raise StateError("invalid sandbox launch record")
    payload = json.dumps(record.to_json(), separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_RECORD_BYTES:
        raise StateError("sandbox launch record exceeds size limit")
    directory = state_root(create=True)
    path = _path(record.launch_id)
    if os.path.lexists(path) and not stat.S_ISREG(path.lstat().st_mode):
        raise StateError(f"{path}: refusing to replace a non-regular file")
    descriptor, temp_name = tempfile.mkstemp(dir=directory, prefix=".sandbox-tmp-",
                                             suffix=".tmp")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
    return record


def get(launch_id: str) -> SandboxLaunch | None:
    if not is_launch_id(launch_id):
        return None
    return _read(state_root() / f"{launch_id}.json")


def list_records() -> list[SandboxLaunch]:
    try:
        directory = state_root()
        if directory.is_symlink() or not directory.is_dir():
            return []
        candidates = sorted(directory.glob("*.json"))
    except OSError:
        return []
    records = [record for record in map(_read, candidates) if record is not None]
    return sorted(records, key=lambda record: (record.created, record.launch_id))


def remove(launch_id: str) -> None:
    try:
        path = _path(launch_id)
        if stat.S_ISREG(path.lstat().st_mode):
            path.unlink()
    except OSError:
        pass


def new(*, launch_id: str, profile: str, harness: str, sandbox: str, mode: str,
        workdir: str, spool: str, gateway: str | None, cwd: str, terminal: str,
        terminal_env: dict) -> SandboxLaunch:
    return SandboxLaunch(
        launch_id=launch_id, profile=profile, harness=harness, sandbox=sandbox,
        mode=mode, workdir=workdir, spool=spool, created=int(time.time()),
        gateway=gateway, cwd=cwd, terminal=terminal, terminal_env=dict(terminal_env),
    )
