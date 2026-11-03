"""Cross-platform focus service for Fleet sessions."""

import dataclasses
import os
import sys

from tongs_agents.discovery import read_session_records, resolve_session_process
from tongs_agents.models import FocusOperation, FocusResult
from tongs_agents.multiplexers import MultiplexerError, PaneRef, get_multiplexer
from tongs_agents.multiplexers.zellij import ACTIVATION_SKIPPED
from tongs_agents.terminal_apis import find_terminal_api
from tongs_agents.terminal_apis._env import is_decimal_id

_DETAILS_LIMIT = 120
_GENERIC_DETAILS = frozenset({"tab or pane selection", "window activation"})
_UNSUPPORTED_PREFIX = "backend does not support "


def _public_record(record):
    return {
        key: value for key, value in record.items()
        if not key.startswith("_")
    }


def _matching_sessions(query):
    records = read_session_records()
    fields = ("canonical_id", "session_id", "repo", "pid")
    exact = [
        record for record in records
        if any(query == str(record.get(field, "")) for field in fields)
    ]
    matches = exact or [
        record for record in records
        if any(query in str(record.get(field, "")) for field in fields)
    ]
    hook_matches = [record for record in matches if not record.get("_process")]
    return hook_matches if hook_matches else matches


def find_session(query):
    matches = _matching_sessions(query)
    if not matches:
        print(f"No session matching '{query}'", file=sys.stderr)
        return None
    if len(matches) == 1:
        return _public_record(matches[0])
    print(f"Multiple sessions match '{query}':", file=sys.stderr)
    for match in matches:
        harness = match.get("harness_id", match.get("agent", "claude"))
        identity = match.get("canonical_id") or match["session_id"]
        print(
            f"  {harness}: {match.get('repo', '?')} "
            f"({match['session_id']}) [{identity}]",
            file=sys.stderr,
        )
    return None


def get_pid(session):
    process = resolve_session_process(session)
    return process.pid if process else None


def _collapsed(text) -> str:
    return " ".join(str(text).replace("\0", "").split())


def _bounded(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _operation_detail(operation: FocusOperation) -> str:
    if operation.succeeded:
        return "ok"
    detail = _collapsed(operation.detail)
    if not operation.attempted and detail.startswith(_UNSUPPORTED_PREFIX):
        return "unsupported"
    if operation.attempted and (not detail or detail in _GENERIC_DETAILS):
        return "failed"
    return detail or "unavailable"


def _step_details(result: FocusResult) -> tuple[str, str]:
    selection = _operation_detail(result.selection)
    activation = _operation_detail(result.activation)
    if result.selection.succeeded or result.activation.succeeded:
        return (
            _bounded(selection, _DETAILS_LIMIT),
            _bounded(activation, _DETAILS_LIMIT),
        )
    # Share one budget so a long selection detail never hides activation.
    half = _DETAILS_LIMIT // 2
    return (
        _bounded(selection, max(half, _DETAILS_LIMIT - len(activation))),
        _bounded(activation, max(half, _DETAILS_LIMIT - len(selection))),
    )


def _details_suffix(result: FocusResult) -> str:
    selection, activation = _step_details(result)
    return f" (selection: {selection}; activation: {activation})"


def _focused(result: FocusResult) -> bool:
    """Complete, or a confirmed pane selection inside the caller's zellij session.

    Inside the same session there is no window to raise, so the skipped
    activation is not a missing step.
    """
    if result.complete:
        return True
    return (
        result.backend == "zellij"
        and result.target_found
        and result.selection.succeeded
        and not result.activation.attempted
        and result.activation.detail == ACTIVATION_SKIPPED
    )


def _pid_suffix(result: FocusResult) -> str:
    return "" if result.pid is None else f" (PID {result.pid})"


def focus_notification(result: FocusResult, repo: str) -> tuple[str, str]:
    if _focused(result):
        return f"Focused: {repo}", "information"
    if result.successful:
        message, severity = f"Partially focused: {repo}", "warning"
    else:
        label = result.state.replace("-", " ").capitalize()
        message, severity = f"Focus {label}: {repo}: {result.reason}", "error"
    if result.successful or result.state == "failed":
        message += _details_suffix(result)
    return message, severity


def _in_session_focus(session) -> FocusResult | None:
    """Select the record's pane when the caller runs in the same zellij session.

    Keyed on the captured pane id rather than the process, so records from
    wrappers whose process identity is unresolved still focus. A record with
    a resolved identity must still name a live process: after the session is
    recreated, its pane id can belong to something unrelated. Returns None
    whenever the record is not provably in the caller's session.
    """
    if not os.environ.get("ZELLIJ"):
        return None
    target = session.get("focus_target")
    if not isinstance(target, dict):
        target = {}
    if target.get("kind") == "desktop":
        return None
    terminal_env = target.get("terminal_env") or session.get("terminal_env")
    if not isinstance(terminal_env, dict):
        return None
    name = terminal_env.get("ZELLIJ_SESSION_NAME")
    pane_id = terminal_env.get("ZELLIJ_PANE_ID")
    if (
        not isinstance(name, str)
        or not name
        or name != os.environ.get("ZELLIJ_SESSION_NAME")
        or not is_decimal_id(pane_id)
    ):
        return None
    socket_dir = terminal_env.get("ZELLIJ_SOCKET_DIR") or None
    if socket_dir is not None and socket_dir != os.environ.get("ZELLIJ_SOCKET_DIR"):
        return None
    pid = None
    if session.get("process_identity") != "unresolved":
        if session.get("status") == "ended":
            return None
        process = resolve_session_process(session)
        if process is None:
            return None
        pid = process.pid
    try:
        multiplexer = get_multiplexer("zellij", socket_dir=socket_dir)
        panes = multiplexer.list_panes(name)
    except MultiplexerError:
        return None
    if not any(pane.ref.pane_id == pane_id for pane in panes):
        return None
    try:
        result = multiplexer.focus(PaneRef("zellij", name, pane_id, socket_dir))
    except MultiplexerError as error:
        result = FocusResult(
            state="failed",
            reason="zellij pane selection failed",
            backend="zellij",
            target_found=True,
            selection=FocusOperation(True, False, error.detail),
        )
    return dataclasses.replace(result, pid=pid)


def focus_session(query) -> FocusResult:
    matches = _matching_sessions(query)
    if not matches:
        return FocusResult(state="not-found", reason=f"no session matching '{query}'")
    if len(matches) > 1:
        identities = ", ".join(
            match.get("canonical_id") or match["session_id"]
            for match in matches
        )
        return FocusResult(
            state="ambiguous",
            reason=f"multiple sessions match '{query}': {identities}",
        )
    session = _public_record(matches[0])
    in_session = _in_session_focus(session)
    if in_session is not None:
        return in_session
    process = resolve_session_process(session)
    if process is None:
        return FocusResult(
            state="unavailable",
            reason="stored process identity is unavailable or stale",
        )

    target = session.get("focus_target", {})
    if not isinstance(target, dict):
        target = {}
    target_kind = target.get("kind")
    terminal_type = target.get("terminal", session.get("terminal", ""))
    terminal_env = target.get("terminal_env", session.get("terminal_env", {}))
    if target_kind == "desktop":
        return FocusResult(
            state="unavailable",
            reason="desktop focus target has no configured backend",
            pid=process.pid,
        )
    if target_kind == "unavailable":
        return FocusResult(
            state="unavailable",
            reason="session focus target is unavailable",
            pid=process.pid,
        )
    if not terminal_type:
        return FocusResult(
            state="unavailable",
            reason="session has no captured terminal metadata",
            pid=process.pid,
        )
    api = find_terminal_api(terminal_type)
    if api is None:
        return FocusResult(
            state="unavailable",
            reason=f"captured terminal backend '{terminal_type}' is unavailable",
            backend=terminal_type,
            pid=process.pid,
        )
    return api.focus_result(process.pid, terminal_env)


def focus(query):
    result = focus_session(query)
    if _focused(result):
        print(f"Focused session via {result.backend}{_pid_suffix(result)}")
        return True
    if result.successful:
        print(
            f"Partially focused session via {result.backend}{_pid_suffix(result)}"
            + _details_suffix(result)
        )
        return True
    message = result.reason
    if result.state == "failed":
        message += _details_suffix(result)
    print(message, file=sys.stderr)
    return False


def main():
    if len(sys.argv) < 2:
        print("Usage: tongs-agents focus <session-id-or-repo>", file=sys.stderr)
        sys.exit(1)
    if not focus(sys.argv[1]):
        sys.exit(1)


if __name__ == "__main__":
    main()
