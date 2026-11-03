"""``tongs-agents web``: watch the workspace read-only from a browser.

Off by default. ``web enable`` needs the pinned Zellij web build, which only
``tongs-agents zellij install --web`` downloads. It then:

1. writes a web server config that tongs-agents owns
   (``state_dir()/web/config.kdl``) binding 127.0.0.1 only; the user's Zellij
   config is never read or changed;
2. starts ``zellij web`` with that config and ``XDG_DATA_HOME`` set to
   ``state_dir()/web/data``, so the server only accepts tokens minted here;
   Zellij keeps them hashed in ``data/zellij/tokens.db`` (0600);
3. mints one read-only token and prints it with the URL once, on stdout only.
   The token itself is never stored, logged or put in an error message;
4. records ``enabled`` in ``state_dir()/web/state.json``. From then on
   ``zellij_binary()`` returns the web build and a new workspace session
   starts with web sharing on (for that session only).

Plain HTTP is used because the listener is loopback only; for a phone or
another machine use an SSH local forward. Read-write tokens are not offered.

``web tokens revoke`` revokes in Zellij and then restarts a running server,
because Zellij 0.45.1 leaves an already-open terminal websocket of a revoked
token open; the restart closes every connection and other viewers reconnect.
``web disable`` stops the server, revokes every token and clears the flag.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

from tongs_agents import paths, zellij_download
from tongs_agents.app_config import DEFAULT_SESSION, AppConfigError, load_app_config
from tongs_agents.commands.doctor import Check
from tongs_agents.multiplexers.zellij_binary import (
    WEB_DIR,
    managed_web_binary,
    read_web_state,
    web_state_path,
    zellij_version,
)

NAME = "web"
HELP = "Watch the workspace read-only from a browser (Zellij web build, 127.0.0.1 only)"
LOOPBACK = "127.0.0.1"
DEFAULT_PORT = 8083
MIN_PORT = 1024
MAX_PORT = 65535
STATE_SCHEMA = "tongs-agents-web/1"
CONFIG_FILE = "config.kdl"
DATA_DIR = "data"
TOKENS_DB = ("zellij", "tokens.db")
INSTALL_COMMAND = "tongs-agents zellij install --web"
EXIT_ERROR = 1
EXIT_UNSUPPORTED = 2
COMMAND_TIMEOUT = 30
STOP_WAIT = 3.0
INHERITED_DROP = ("ZELLIJ", "ZELLIJ_SESSION_NAME", "ZELLIJ_PANE_ID")
TOKEN_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_MINTED = re.compile(r"^(token_[0-9]{1,9}): (\S+) \(read-only\)\s*$", re.MULTILINE)
_SECRET_AFTER_NAME = re.compile(r"(\btoken_[0-9]{1,9}:[ \t]+)(?!created at)\S+")
_UUID = re.compile(r"\b[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
                   r"[0-9A-Fa-f]{12}\b")
_BIND = re.compile(r'^\s*web_server_ip\s+"([^"\n]*)"', re.MULTILINE)
REDACTED = "<redacted>"


class WebError(RuntimeError):
    """A web step failed; the message never carries a token."""


def redact(text: str) -> str:
    """``text`` with anything shaped like a Zellij login token replaced."""
    return _UUID.sub(REDACTED, _SECRET_AFTER_NAME.sub(rf"\g<1>{REDACTED}", text or ""))


def _port(value: str) -> int:
    try:
        port = int(value, 10)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r:.20} is not a port number") from None
    if not MIN_PORT <= port <= MAX_PORT:
        raise argparse.ArgumentTypeError(f"port must be from {MIN_PORT} to {MAX_PORT}")
    return port


def _token_name(value: str) -> str:
    if not TOKEN_NAME_RE.match(value):
        raise argparse.ArgumentTypeError(f"refused token name {value!r:.40}")
    return value


def add_arguments(parser):
    actions = parser.add_subparsers(dest="web_action")
    actions.add_parser("status", help="Show whether web watching is on (default)")
    enable = actions.add_parser(
        "enable",
        help="Start the loopback web server and print a new read-only token once",
    )
    enable.add_argument(
        "--port", type=_port, default=None,
        help=f"Port on 127.0.0.1 (default: the last one used, else {DEFAULT_PORT})",
    )
    actions.add_parser("disable", help="Stop the web server and revoke every token")
    tokens = actions.add_parser("tokens", help="List or revoke read-only tokens")
    token_actions = tokens.add_subparsers(dest="tokens_action")
    token_actions.add_parser("list", help="List token names and dates (never the tokens)")
    revoke = token_actions.add_parser(
        "revoke", help="Revoke a token and close every open web connection",
    )
    target = revoke.add_mutually_exclusive_group(required=True)
    target.add_argument("name", nargs="?", type=_token_name, help="Token name, e.g. token_1")
    target.add_argument("--all", action="store_true", help="Revoke every token")


def web_dir(create: bool = False) -> Path:
    path = paths.state_dir(create) / WEB_DIR
    if create:
        paths.ensure_private_dir(path)
    return path


def config_path() -> Path:
    return web_dir() / CONFIG_FILE


def data_dir(create: bool = False) -> Path:
    path = web_dir(create) / DATA_DIR
    if create:
        paths.ensure_private_dir(path)
    return path


def tokens_db() -> Path:
    return data_dir().joinpath(*TOKENS_DB)


def render_config(port: int) -> str:
    return (
        "// Written by tongs-agents web enable; used only by the web server it starts.\n"
        f'web_server_ip "{LOOPBACK}"\n'
        f"web_server_port {int(port)}\n"
    )


def configured_bind(text: str) -> str | None:
    match = _BIND.search(text or "")
    return match.group(1) if match else None


def _write_private(path: Path, text: str) -> None:
    """Atomically replace ``path`` with ``text`` at mode 0600."""
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def write_state(enabled: bool, port: int) -> None:
    web_dir(create=True)
    state = {"schema": STATE_SCHEMA, "enabled": enabled, "bind": LOOPBACK, "port": port}
    _write_private(web_state_path(), json.dumps(state, indent=2) + "\n")


def _state_port(state: dict) -> int | None:
    port = state.get("port")
    if isinstance(port, int) and not isinstance(port, bool) and MIN_PORT <= port <= MAX_PORT:
        return port
    return None


def zellij_env(base=None) -> dict[str, str]:
    """The env for every ``zellij web`` call: the token store under state_dir()."""
    env = dict(os.environ if base is None else base)
    for key in INHERITED_DROP:
        env.pop(key, None)
    env["XDG_DATA_HOME"] = str(data_dir())
    return env


def _call(binary: str, *arguments: str, daemon: bool = False) -> tuple[int, str]:
    """Run ``binary --config <owned config> web ...``; return (status, redacted output).

    A daemonizing start writes to a private file instead of a pipe, so the
    server that keeps running never holds a pipe this call waits on.
    """
    argv = [binary, "--config", str(config_path()), "web", *arguments]
    try:
        if daemon:
            with tempfile.TemporaryFile(dir=web_dir()) as output:
                result = subprocess.run(
                    argv, stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                    env=zellij_env(), timeout=COMMAND_TIMEOUT, start_new_session=True,
                )
                output.seek(0)
                text = output.read().decode("utf-8", "replace")
        else:
            result = subprocess.run(
                argv, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                env=zellij_env(), timeout=COMMAND_TIMEOUT,
            )
            text = (result.stdout or "") + (result.stderr or "")
    except (OSError, subprocess.TimeoutExpired) as error:
        raise WebError(f"zellij web {arguments[0]} failed: {type(error).__name__}") from None
    return result.returncode, text


def _read_cmdline(pid: str) -> list[str]:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as stream:
            raw = stream.read()
    except OSError:
        return []
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]


def owned_server_pids() -> list[int]:
    """Running ``zellij web --start`` servers that use the config tongs-agents owns."""
    config = str(config_path())
    found = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return found
    uid = os.getuid()
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            if os.stat(f"/proc/{entry}").st_uid != uid:
                continue
        except OSError:
            continue
        argv = _read_cmdline(entry)
        if not argv or "--start" not in argv or "web" not in argv:
            continue
        for index, value in enumerate(argv[:-1]):
            if value == "--config" and argv[index + 1] == config:
                found.append(int(entry))
                break
    return sorted(found)


def _wait_gone(timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while owned_server_pids():
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)
    return True


def stop_server(binary: str | None) -> bool:
    """Stop the server tongs-agents started, if one runs; True when none is left."""
    if not owned_server_pids():
        return True
    if binary is not None and config_path().is_file():
        try:
            _call(binary, "--stop")
        except WebError:
            pass
        if _wait_gone(STOP_WAIT):
            return True
    for pid in owned_server_pids():
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    return _wait_gone(STOP_WAIT)


def start_server(binary: str) -> None:
    code, output = _call(binary, "--start", "--daemonize", daemon=True)
    if code != 0 or not owned_server_pids():
        detail = output.strip().splitlines()[-1] if output.strip() else f"exit {code}"
        raise WebError(f"the web server did not start: {detail[:200]}")


def mint_read_only_token(binary: str) -> tuple[str, str]:
    """(name, token) of a new read-only token; the token is returned, never printed."""
    code, output = _call(binary, "--create-read-only-token")
    match = _MINTED.search(output)
    if code != 0 or match is None:
        detail = redact(output.strip())
        if "already exists" in detail:
            detail += (" (a Zellij 0.45.1 naming quirk after revoking one of several "
                       "tokens; run 'tongs-agents web tokens revoke --all' and enable again)")
        raise WebError(f"could not create a read-only token: {detail[:300] or f'exit {code}'}")
    return match.group(1), match.group(2)


def _secure_tokens_db() -> None:
    path = tokens_db()
    try:
        info = os.lstat(path)
    except OSError:
        return
    if info.st_mode & 0o077:
        os.chmod(path, 0o600)


def _web_build_problem(binary: str) -> str | None:
    """Why the managed web build must not be used, or None."""
    try:
        pin = zellij_download.select_pin(flavor=zellij_download.WEB)
    except zellij_download.DownloadError as error:
        return str(error)
    try:
        digest = zellij_download.file_sha256(Path(binary))
    except OSError as error:
        return f"cannot read {binary}: {error.strerror or error}"
    if digest != pin.binary_sha256:
        return (f"{binary} does not match the pinned web build {pin.version}; "
                f"run '{INSTALL_COMMAND}' again")
    return None


def workspace_session() -> str:
    try:
        return load_app_config().workspace.session
    except AppConfigError:
        return DEFAULT_SESSION


def watch_url(port: int, session: str) -> str:
    return f"http://{LOOPBACK}:{port}/{urllib.parse.quote(session, safe='')}"


def _error(message: str, code: int = EXIT_ERROR) -> int:
    print(f"tongs-agents web: {redact(message)}", file=sys.stderr)
    return code


def _supported() -> bool:
    return zellij_download.platform_key() is not None


def enable(port: int | None = None) -> int:
    if not _supported():
        return _error("unsupported: the Zellij web build is pinned for Linux x86_64 only",
                      EXIT_UNSUPPORTED)
    binary = managed_web_binary()
    if binary is None:
        return _error(
            f"the Zellij web build is not installed; run '{INSTALL_COMMAND}' first "
            "(it downloads the pinned build from GitHub)"
        )
    problem = _web_build_problem(binary)
    if problem is not None:
        return _error(problem)
    state = read_web_state()
    previous = _state_port(state)
    port = port or previous or DEFAULT_PORT
    started = False
    try:
        web_dir(create=True)
        data_dir(create=True)
        if owned_server_pids() and (previous != port or not config_path().is_file()):
            if not stop_server(binary):
                return _error("the running web server did not stop; not changing its port")
        _write_private(config_path(), render_config(port))
        if not owned_server_pids():
            start_server(binary)
            started = True
        name, token = mint_read_only_token(binary)
        _secure_tokens_db()
        write_state(True, port)
    except (WebError, paths.UnsafePathError, OSError) as error:
        if started:
            stop_server(binary)
        return _error(str(error))
    session = workspace_session()
    print(f"Web watching is on at {watch_url(port, session)} (127.0.0.1 only, plain HTTP).")
    print(f"Read-only token {name}, shown once and not stored: {token}")
    print(f"From another device: ssh -L {port}:{LOOPBACK}:{port} <this host>, "
          "then open the URL there.")
    print(f"A workspace session started before web watching was enabled is not shared; "
          f"quit {session!r} and run 'tongs-agents workspace' again to watch it.")
    return 0


def disable() -> int:
    binary = managed_web_binary()
    stopped = stop_server(binary)
    revoked = None
    if binary is not None and tokens_db().exists():
        try:
            code, output = _call(binary, "--revoke-all-tokens")
            revoked = code == 0
        except WebError:
            revoked = False
    if revoked is not True and tokens_db().exists():
        # Without the web build (or when its revoke failed) the token store
        # would come back to life on the next enable; it is ours, so drop it.
        try:
            tokens_db().unlink()
            revoked = True
        except OSError:
            revoked = False
    state = read_web_state()
    try:
        write_state(False, _state_port(state) or DEFAULT_PORT)
    except (paths.UnsafePathError, OSError) as error:
        return _error(f"cannot record that web watching is off: {error}")
    print("Web watching is off. New workspace sessions use the usual zellij.")
    if revoked is False:
        print(f"Could not revoke the tokens in {tokens_db()}; delete that file to revoke them.")
    if not stopped:
        return _error("the web server tongs-agents started is still running")
    return 0


def _require_binary(verb: str) -> str | None:
    binary = managed_web_binary()
    if binary is None:
        print(f"tongs-agents web tokens {verb}: the Zellij web build is not installed",
              file=sys.stderr)
    return binary


def list_tokens() -> int:
    binary = _require_binary("list")
    if binary is None:
        return EXIT_ERROR
    if not tokens_db().exists():
        print("No tokens.")
        return 0
    try:
        code, output = _call(binary, "--list-tokens")
    except WebError as error:
        return _error(str(error))
    text = redact(output).strip()
    print(text or "No tokens.")
    return 0 if code == 0 else EXIT_ERROR


def revoke_tokens(name: str | None, every: bool) -> int:
    binary = _require_binary("revoke")
    if binary is None:
        return EXIT_ERROR
    arguments = ("--revoke-all-tokens",) if every else ("--revoke-token", name)
    try:
        code, output = _call(binary, *arguments)
    except WebError as error:
        return _error(str(error))
    if code != 0:
        return _error(f"zellij refused the revoke: {output.strip()[:200]}")
    print(redact(output).strip() or "Revoked.")
    if owned_server_pids():
        if not stop_server(binary):
            return _error("revoked, but the web server did not stop; run 'tongs-agents web "
                          "disable' to close open connections")
        try:
            start_server(binary)
        except WebError as error:
            return _error(f"revoked and closed every connection, but {error}")
        print("Restarted the web server to close every open connection; viewers with "
              "other tokens reconnect.")
    return 0


def status() -> int:
    state = read_web_state()
    enabled = state.get("enabled") is True
    port = _state_port(state) or DEFAULT_PORT
    binary = managed_web_binary()
    running = bool(owned_server_pids())
    print(f"Web watching: {'on' if enabled else 'off'}")
    if binary is None:
        print(f"Web build: not installed (run '{INSTALL_COMMAND}')")
    else:
        version = zellij_version(binary)
        shown = ".".join(str(part) for part in version) if version else "unknown version"
        print(f"Web build: zellij {shown} at {binary}")
    if enabled or running:
        print(f"Server: {'running' if running else 'not running'} on {LOOPBACK}:{port}")
    if enabled and running:
        print(f"URL: {watch_url(port, workspace_session())}")
    return 0


def run(args) -> int:
    action = getattr(args, "web_action", None)
    if action == "enable":
        return enable(args.port)
    if action == "disable":
        return disable()
    if action == "tokens":
        verb = getattr(args, "tokens_action", None)
        if verb == "revoke":
            return revoke_tokens(args.name, args.all)
        return list_tokens()
    return status()


def doctor_checks():
    """Only reads: the web state, the owned config's bind and file modes."""
    checks = []
    state = read_web_state()
    enabled = state.get("enabled") is True
    config = config_path()
    try:
        bind = configured_bind(config.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        bind = None
    recorded = state.get("bind")
    for where, value in (("web server config", bind), ("web state", recorded)):
        if value is not None and value != LOOPBACK:
            checks.append(Check(
                "web-bind", "warn",
                f"The {where} binds {str(value)[:64]!r}, not {LOOPBACK}; tongs-agents "
                "serves the web client on loopback only",
                "tongs-agents web disable && tongs-agents web enable",
            ))
    if not enabled:
        if owned_server_pids():
            checks.append(Check(
                "web", "warn", "Web watching is off but its web server is still running",
                "tongs-agents web disable",
            ))
        elif not checks:
            checks.append(Check("web", "ok", "Web watching is off"))
        return checks
    if managed_web_binary() is None:
        checks.append(Check(
            "web", "warn",
            "Web watching is enabled but the Zellij web build is not installed; "
            "workspaces start without web sharing",
            INSTALL_COMMAND,
        ))
    try:
        mode = os.lstat(tokens_db()).st_mode & 0o777
    except OSError:
        mode = None
    if mode is not None and mode & 0o077:
        checks.append(Check(
            "web-tokens", "warn",
            f"The web token store {tokens_db()} is mode {mode:o}, not 600",
            f"chmod 600 {tokens_db()}",
        ))
    if not any(check.level == "warn" for check in checks):
        port = _state_port(state) or DEFAULT_PORT
        checks.append(Check("web", "ok", f"Web watching is on at {LOOPBACK}:{port}"))
    return checks
