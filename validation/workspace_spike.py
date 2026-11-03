"""Spike the zellij workspace mechanics tongs-agents will rely on.

Local-only Linux helper; it is never run in CI and it is not product code.
It answers the M1-0 (#7) mechanics questions against one zellij binary taken
from --zellij-dir, which must be an extracted upstream release, never an
installed copy. Every run uses a private HOME, XDG config/state/cache/data and
runtime dirs, TMPDIR, ZELLIJ_SOCKET_DIR and ZELLIJ_CONFIG_DIR under a fresh
temporary directory, drives interactive clients through private PTYs, creates
only uniquely named sessions in the private socket dir, and signals only
processes it started (PID plus kernel start time). The tmux step uses a
private tmux socket and an empty config. It never reads or edits user
configuration, never attaches to user sessions, and the JSON it prints is
redacted to placeholders.
"""

import argparse
import json
import os
from pathlib import Path
import secrets
import shlex
import shutil
import sys
import tempfile
import time

from probe_terminals import (
    PtyClient,
    Redactor,
    Tracker,
    config_snapshot,
    platform_facts,
    process_cmdline,
    run,
    typed_target,
    wait_until,
    zellij_cache_mentions,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "workspace-spike/1"
SIDEBAR_COLUMNS = 30
CLIENT_ROWS = 40
CLIENT_COLUMNS = 120
PRIVATE_DIRS = ("home", "cfg", "cfg-ser", "xdg-config", "xdg-state", "cache", "data", "run",
                "tmp", "sock", "io", "cwd", "bin", "tmux")
STRIPPED_PREFIXES = ("ZELLIJ", "TMUX", "KITTY_", "WEZTERM_", "TERMINATOR_", "SPIKE_")

AGENT = """\
import json
import os
import sys
import time

io_dir, tag = sys.argv[1], sys.argv[2]
names = sorted(name for name in os.environ if name.startswith(("ZELLIJ", "TMUX", "SPIKE_")))
record = {"pid": os.getpid(), "cwd": os.getcwd(), "time": time.time(),
          "env": {name: os.environ[name] for name in names}}
with open(os.path.join(io_dir, "starts-" + tag), "a") as handle:
    handle.write(json.dumps(record) + "\\n")
if len(sys.argv) > 3:
    sys.exit(int(sys.argv[3]))
for line in sys.stdin:
    with open(os.path.join(io_dir, "in-" + tag), "a") as handle:
        handle.write(line)
"""

RUNNER = """\
import json
import os
import subprocess
import sys
import time

io_dir, tag = sys.argv[1], sys.argv[2]
names = sorted(name for name in os.environ if name.startswith(("ZELLIJ", "TMUX", "SPIKE_")))
record = {"pid": os.getpid(), "cwd": os.getcwd(), "time": time.time(),
          "env": {name: os.environ[name] for name in names}}
with open(os.path.join(io_dir, "starts-" + tag), "a") as handle:
    handle.write(json.dumps(record) + "\\n")
count = 0
while True:
    request = os.path.join(io_dir, "req-%s-%d.json" % (tag, count))
    if not os.path.exists(request):
        time.sleep(0.05)
        continue
    with open(request) as handle:
        spec = json.load(handle)
    env = dict(os.environ)
    env.update(spec.get("env", {}))
    try:
        done = subprocess.run(spec["argv"], env=env, capture_output=True, text=True,
                              timeout=10, stdin=subprocess.DEVNULL)
        result = {"code": done.returncode, "stdout": done.stdout, "stderr": done.stderr}
    except (OSError, subprocess.TimeoutExpired) as error:
        result = {"code": None, "stdout": "", "stderr": type(error).__name__}
    response = os.path.join(io_dir, "resp-%s-%d.json" % (tag, count))
    with open(response + ".tmp", "w") as handle:
        json.dump(result, handle)
    os.replace(response + ".tmp", response)
    count += 1
"""

CAPTURE = """\
import json
import os
import sys

from tongs_agents.terminal_apis import capture_terminal_info, detect_terminal_from_env

io_dir, tag = sys.argv[1], sys.argv[2]
info = capture_terminal_info()
record = {
    "terminal": info["terminal"],
    "terminal_env_keys": sorted(info["terminal_env"]),
    "env_only_terminal": detect_terminal_from_env().name,
    "markers_present": sorted(name for name in os.environ
                              if name.startswith(("ZELLIJ", "TMUX"))),
}
with open(os.path.join(io_dir, "capture-" + tag + ".tmp"), "w") as handle:
    json.dump(record, handle)
os.replace(os.path.join(io_dir, "capture-" + tag + ".tmp"),
           os.path.join(io_dir, "capture-" + tag))
for line in sys.stdin:
    pass
"""


def key_types(items):
    """Union of JSON field names with the value types observed for each."""
    fields = {}
    for item in items:
        for key, value in item.items():
            kind = "null" if value is None else type(value).__name__
            fields.setdefault(key, set()).add(kind)
    return {key: sorted(kinds) for key, kinds in sorted(fields.items())}


def starts(io_dir, tag):
    try:
        lines = (io_dir / f"starts-{tag}").read_text().splitlines()
    except OSError:
        return []
    return [json.loads(line) for line in lines if line.strip()]


class Spike:
    def __init__(self, root, token, zellij_dir):
        self.root = root
        self.token = token
        self.zellij = str(zellij_dir / "zellij")
        self.io = root / "io"
        self.tracker = Tracker([str(root), token])
        self.sessions = []
        self.redact = Redactor.for_host({
            str(root): "<spike_tmp>", token: "<token>", str(zellij_dir): "<zellij_dir>",
            sys.executable: "<python>", str(REPO_ROOT): "<repo>",
        })
        for name in PRIVATE_DIRS:
            (root / name).mkdir(mode=0o700)
        base = "show_startup_tips false\nshow_release_notes false\n"
        (root / "cfg" / "config.kdl").write_text(base + "session_serialization false\n")
        (root / "cfg-ser" / "config.kdl").write_text(
            base + "session_serialization true\nserialization_interval 1\n")
        for name, text in (("agent.py", AGENT), ("runner.py", RUNNER),
                           ("capture.py", CAPTURE)):
            (root / "bin" / name).write_text(text)
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(STRIPPED_PREFIXES) and key not in ("STY", "WINDOW")}
        env.update({
            "HOME": str(root / "home"), "TERM": "xterm-256color", "SHELL": "/bin/sh",
            "PATH": f"{zellij_dir}{os.pathsep}{env.get('PATH', '')}",
            "XDG_CONFIG_HOME": str(root / "xdg-config"),
            "XDG_STATE_HOME": str(root / "xdg-state"),
            "XDG_CACHE_HOME": str(root / "cache"), "XDG_DATA_HOME": str(root / "data"),
            "XDG_RUNTIME_DIR": str(root / "run"), "TMPDIR": str(root / "tmp"),
            "ZELLIJ_SOCKET_DIR": str(root / "sock"), "ZELLIJ_CONFIG_DIR": str(root / "cfg"),
            "SPIKE_ORIGIN": "launcher",
        })
        self.env = env

    def session_name(self, label):
        name = f"{self.token}-{label}"
        self.sessions.append(name)
        return name

    def env_with(self, **extra):
        env = dict(self.env)
        env.update(extra)
        return env

    def zj(self, *arguments, env=None, timeout=5):
        return run([self.zellij, *arguments], env=env or self.env, timeout=timeout)

    def action(self, session, *arguments, env=None):
        return self.zj("--session", session, "action", *arguments, env=env)

    def py(self, script, *arguments):
        return [sys.executable, "-I", str(self.root / "bin" / script), str(self.io), *arguments]

    def kdl_command(self, name, script, *arguments, extra=""):
        argv = self.py(script, *arguments)
        args = " ".join(json.dumps(item) for item in argv[1:])
        return (f'pane name="{name}" command={json.dumps(argv[0])} {extra}'
                f'cwd={json.dumps(str(self.root / "cwd"))} {{ args {args}; }}')

    def layout(self, label, tag):
        """The static workspace template: sidebar plus one agent pane and a tongs tab."""
        sidebar = self.kdl_command("sidebar", "runner.py", f"{tag}-sidebar",
                                   extra=f"size={SIDEBAR_COLUMNS} ")
        main = self.kdl_command("main", "agent.py", f"{tag}-main")
        tongs = self.kdl_command("tongs", "agent.py", f"{tag}-tongs")
        path = self.root / f"layout-{label}.kdl"
        path.write_text(
            "layout {\n"
            ' tab name="agents" focus=true {\n'
            '  pane split_direction="vertical" {\n'
            f"   {sidebar}\n   {main}\n  }}\n }}\n"
            f' tab name="tongs" {{\n  {tongs}\n }}\n'
            "}\n")
        return path

    def client(self, argv, env=None, label="client"):
        return PtyClient([self.zellij, *argv], env or self.env, str(self.root / "cwd"),
                         self.tracker, label)

    def clients(self, session):
        code, out, _ = self.action(session, "list-clients")
        if code != 0:
            return None
        lines = out.splitlines()
        return {"header": lines[0].split() if lines else [],
                "rows": [line.split() for line in lines[1:] if line.strip()]}

    def client_panes(self, session):
        listed = self.clients(session) or {"rows": []}
        return {row[0]: row[1] for row in listed["rows"] if len(row) >= 2}

    def panes(self, session):
        code, out, err = self.action(session, "list-panes", "--json", "--all")
        if code != 0 or not out:
            return None
        try:
            return json.loads(out)
        except ValueError:
            return None

    def sessions_listing(self):
        listing = {}
        for flag in ("-n", "-s"):
            code, out, err = self.zj("list-sessions", flag)
            listing[flag] = {"exit": code, "lines": out.splitlines(), "stderr": err[:200]}
        return listing

    def wait_started(self, tag, count=1, timeout=20):
        return wait_until(lambda: len(starts(self.io, tag)) >= count, timeout)

    def sidebar_run(self, tag, argv, env=None, timeout=15):
        """Run argv inside the sidebar pane process, as the real sidebar would."""
        index = self.requests_for(tag)
        request = self.io / f"req-{tag}-{index}.json"
        request.with_suffix(".tmp").write_text(json.dumps({"argv": argv, "env": env or {}}))
        os.replace(request.with_suffix(".tmp"), request)
        response = self.io / f"resp-{tag}-{index}.json"
        if not wait_until(response.exists, timeout):
            return {"code": None, "stdout": "", "stderr": "sidebar did not answer"}
        return json.loads(response.read_text())

    def requests_for(self, tag):
        return len(list(self.io.glob(f"req-{tag}-*.json")))

    def pane_for(self, panes, pane_id):
        for item in panes or []:
            if not item.get("is_plugin") and item.get("id") == pane_id:
                return item
        return None

    def cleanup(self):
        for session in self.sessions:
            self.zj("kill-session", session)
            self.zj("delete-session", "--force", session)
        run(["tmux", "-S", str(self.root / "tmux" / "sock"), "kill-server"])
        summary = self.tracker.terminate()
        time.sleep(1)
        late = self.tracker.terminate()
        summary["survivors"] = late["survivors"]
        summary["marker_strays"] = late["marker_strays"]
        return summary


def returned_id(stdout):
    text = stdout.strip()
    kind, _, number = text.partition("_")
    return {"raw_shape": "terminal_<digits>" if kind == "terminal" and number.isdigit()
            else "plugin_<digits>" if kind == "plugin" and number.isdigit() else text[:80],
            "pane_id": int(number) if number.isdigit() else None}


def step_create(spike):
    """Exact create argv with a layout file, attach --create and duplicates."""
    result = {}
    name = spike.session_name("main")
    layout = spike.layout("main", "m")
    argv = ["--config-dir", str(spike.root / "cfg"), "--session", name,
            "--new-session-with-layout", str(layout)]
    spike.main_client = spike.client(argv, spike.env_with(SPIKE_ORIGIN="server-creator"))
    ready = spike.wait_started("m-sidebar") and spike.wait_started("m-main")
    result["new_session_with_layout"] = {
        "argv": ["zellij", "--session", "<session>", "--new-session-with-layout",
                 "<layout.kdl>"],
        "ready": bool(ready),
        "tabs_started": {tag: bool(starts(spike.io, f"m-{tag}"))
                         for tag in ("sidebar", "main", "tongs")},
    }
    spike.main = name
    wait_until(lambda: spike.client_panes(name), 10)

    code, out, err = spike.zj("--session", name, "--new-session-with-layout", str(layout),
                              timeout=5)
    result["new_session_with_layout_existing_name"] = {
        "exit": code, "stdout": spike.redact(out)[:200], "stderr": spike.redact(err)[:300],
        "note": "run without a TTY against a session that already exists"}

    background = spike.session_name("bg")
    code, out, err = spike.zj("--layout", str(spike.layout("bg", "b")), "attach",
                              "--create-background", background, timeout=10)
    wait_until(lambda: starts(spike.io, "b-sidebar"), 10)
    result["layout_then_attach_create_background"] = {
        "argv": ["zellij", "--layout", "<layout.kdl>", "attach", "--create-background",
                 "<session>"],
        "exit": code, "stderr": spike.redact(err)[:300],
        "layout_applied": bool(starts(spike.io, "b-sidebar")),
        "pane_names": sorted(item.get("title", "") for item in spike.panes(background) or []
                             if not item.get("is_plugin")),
    }
    code, out, err = spike.zj("attach", "--create-background", background, timeout=10)
    result["attach_create_background_existing"] = {
        "exit": code, "stdout": spike.redact(out)[:200], "stderr": spike.redact(err)[:300]}

    default_layout = spike.session_name("opt")
    code, out, err = spike.zj("attach", "--create-background", default_layout, "options",
                              "--default-layout", str(spike.layout("opt", "o")), timeout=10)
    wait_until(lambda: starts(spike.io, "o-sidebar"), 10)
    result["attach_create_background_options_default_layout"] = {
        "argv": ["zellij", "attach", "--create-background", "<session>", "options",
                 "--default-layout", "<layout.kdl>"],
        "exit": code, "stderr": spike.redact(err)[:300],
        "layout_applied": bool(starts(spike.io, "o-sidebar")),
    }

    plain = spike.session_name("plain")
    client = spike.client(["attach", "--create", plain])
    created = wait_until(lambda: spike.client_panes(plain), 40)
    wait_until(lambda: [item for item in spike.panes(plain) or [] if not item.get("is_plugin")],
               10)
    panes = spike.panes(plain) or []
    result["attach_create_new"] = {
        "argv": ["zellij", "attach", "--create", "<session>"],
        "created": bool(created),
        "terminal_panes": len([item for item in panes if not item.get("is_plugin")]),
        "pane_commands": sorted({str(item.get("pane_command")) for item in panes
                                 if not item.get("is_plugin")}),
    }
    known = {line.strip() for line in spike.zj("list-sessions", "-s")[1].splitlines()}
    second = spike.client(["--layout", str(spike.layout("plain", "p")), "attach", "--create",
                           plain])
    wait_until(lambda: len(spike.client_panes(plain)) >= 2, 10)
    time.sleep(2)
    now = {line.strip() for line in spike.zj("list-sessions", "-s")[1].splitlines()}
    extra = sorted(now - known)
    spike.sessions.extend(extra)
    result["layout_then_attach_create_existing"] = {
        "argv": ["zellij", "--layout", "<layout.kdl>", "attach", "--create", "<session>"],
        "clients_on_existing": len(spike.client_panes(plain)),
        "layout_tab_added_or_started": bool(starts(spike.io, "p-sidebar")),
        "new_sessions_created": len(extra),
    }
    code, out, err = spike.zj("--session", plain, "--layout",
                              str(spike.layout("plain-tab", "q")), timeout=10)
    wait_until(lambda: starts(spike.io, "q-sidebar"), 10)
    result["session_layout_adds_tabs"] = {
        "argv": ["zellij", "--session", "<session>", "--layout", "<layout.kdl>"],
        "exit": code, "stdout": spike.redact(out)[:200], "stderr": spike.redact(err)[:200],
        "layout_started": bool(starts(spike.io, "q-sidebar")),
        "tab_names": sorted({str(item.get("tab_name")) for item in spike.panes(plain) or []}),
    }
    for item in (client, second):
        spike.tracker.add(item.pid, "client")
    spike.zj("kill-session", plain)
    return result


def step_spawn(spike):
    """new-pane stdout, env inheritance, ZELLIJ_PANE_ID and focus, outside and inside."""
    session = spike.main
    result = {}
    before = spike.client_panes(session)
    outside_env = spike.env_with(SPIKE_ORIGIN="caller-outside", SPIKE_CALLER_ONLY="1")
    code, out, err = spike.action(
        session, "new-pane", "--name", "out-agent", "--cwd", str(spike.root / "cwd"), "--",
        *spike.py("agent.py", "out-agent"), env=outside_env)
    spike.wait_started("out-agent")
    record = (starts(spike.io, "out-agent") or [{}])[0]
    parsed = returned_id(out)
    result["outside_with_session"] = {
        "argv": ["zellij", "--session", "<session>", "action", "new-pane", "--name", "<name>",
                 "--cwd", "<dir>", "--", "<argv>..."],
        "exit": code, "stdout_shape": parsed["raw_shape"],
        "stderr": spike.redact(err)[:200],
        "zellij_pane_id_env": record.get("env", {}).get("ZELLIJ_PANE_ID"),
        "pane_id_matches_env": (parsed["pane_id"] is not None and str(parsed["pane_id"])
                                == record.get("env", {}).get("ZELLIJ_PANE_ID")),
        "cwd_applied": record.get("cwd") == str(spike.root / "cwd"),
        "spike_origin_in_pane": record.get("env", {}).get("SPIKE_ORIGIN"),
        "caller_only_variable_in_pane": "SPIKE_CALLER_ONLY" in record.get("env", {}),
        "client_focus_before": before,
        "client_focus_after": spike.client_panes(session),
        "pane_title": (spike.pane_for(spike.panes(session), parsed["pane_id"]) or {}).get(
            "title"),
    }
    spike.out_pane = parsed["pane_id"]

    sidebar = "m-sidebar"
    before = spike.client_panes(session)
    response = spike.sidebar_run(
        sidebar, ["zellij", "action", "new-pane", "--name", "in-agent", "--",
                  *spike.py("agent.py", "in-agent")],
        env={"SPIKE_ORIGIN": "caller-inside", "SPIKE_CALLER_ONLY": "1"})
    spike.wait_started("in-agent")
    record = (starts(spike.io, "in-agent") or [{}])[0]
    parsed = returned_id(response["stdout"])
    result["inside_from_sidebar"] = {
        "argv": ["zellij", "action", "new-pane", "--name", "<name>", "--", "<argv>..."],
        "exit": response["code"], "stdout_shape": parsed["raw_shape"],
        "stdout_trailing_newline": response["stdout"].endswith("\n"),
        "stderr": spike.redact(response["stderr"])[:200],
        "pane_id_matches_env": (parsed["pane_id"] is not None and str(parsed["pane_id"])
                                == record.get("env", {}).get("ZELLIJ_PANE_ID")),
        "spike_origin_in_pane": record.get("env", {}).get("SPIKE_ORIGIN"),
        "caller_only_variable_in_pane": "SPIKE_CALLER_ONLY" in record.get("env", {}),
        "client_focus_before": before,
        "client_focus_after": spike.client_panes(session),
    }

    before = spike.client_panes(session)
    response = spike.sidebar_run(
        sidebar, ["zellij", "action", "new-pane", "--no-focus", "--name", "nofocus-agent",
                  "--", *spike.py("agent.py", "nofocus-agent")])
    started = spike.wait_started("nofocus-agent", timeout=5)
    result["inside_no_focus"] = {
        "exit": response["code"], "stdout_shape": returned_id(response["stdout"])["raw_shape"],
        "stderr": spike.redact(response["stderr"])[:300],
        "pane_started": bool(started),
        "client_focus_before": before,
        "client_focus_after": spike.client_panes(session),
    }

    spike.action(session, "focus-pane-id", "terminal_0")
    before = spike.client_panes(session)
    response = spike.sidebar_run(
        sidebar, ["zellij", "action", "new-pane", "--near-current-pane", "--name",
                  "near-agent", "--", *spike.py("agent.py", "near-agent")])
    near = spike.wait_started("near-agent", timeout=5)
    result["inside_near_current_pane"] = {
        "exit": response["code"], "pane_started": bool(near), "stderr": spike.redact(response["stderr"])[:200],
        "client_focus_before": before,
        "client_focus_after": spike.client_panes(session),
    }
    code, out, err = spike.action(session, "new-pane", "--tab-id", "999", "--", "true")
    result["unknown_tab_id"] = {"exit": code, "stdout": spike.redact(out)[:120],
                                "stderr": spike.redact(err)[:200]}
    code, out, err = spike.zj("--session", f"{spike.token}-missing", "action", "new-pane",
                              "--", "true")
    result["unknown_session"] = {"exit": code, "stdout": spike.redact(out)[:120],
                                 "stderr": spike.redact(err)[:200]}
    return result


def step_listings(spike):
    """list-panes --json schema, list-clients and list-sessions shapes."""
    session = spike.main
    panes = spike.panes(session) or []
    terminals = [item for item in panes if not item.get("is_plugin")]
    plugins = [item for item in panes if item.get("is_plugin")]
    code, out, _ = spike.action(session, "list-panes", "--json")
    default_fields = key_types(json.loads(out)) if code == 0 and out else None
    code, out, _ = spike.action(session, "list-panes")
    table_header = out.splitlines()[0].split() if code == 0 and out else None
    listed = spike.clients(session)
    sample = next((item for item in terminals if item.get("title") == "out-agent"), None)
    return {
        "list_panes_json_all_fields": key_types(panes),
        "list_panes_json_terminal_fields": key_types(terminals),
        "list_panes_json_plugin_fields": key_types(plugins),
        "list_panes_json_default_fields": default_fields,
        "list_panes_table_header": table_header,
        "terminal_pane_example": {key: spike.redact(value) if isinstance(value, str) else value
                                  for key, value in (sample or {}).items()},
        "list_clients_header": listed["header"] if listed else None,
        "list_clients_row_widths": sorted({len(row) for row in listed["rows"]})
        if listed else None,
        "list_clients_rows": [[spike.redact(cell) for cell in row]
                              for row in (listed or {"rows": []})["rows"]],
        "list_sessions": {flag: {**value, "lines": [spike.redact(line)
                                                    for line in value["lines"]]}
                          for flag, value in spike.sessions_listing().items()},
    }


def step_exit(spike):
    """Close-on-exit behaviour and visibility of an exec failure."""
    session = spike.main
    result = {}
    cases = (
        ("exit3", ["new-pane", "--name", "exit3", "--", *spike.py("agent.py", "exit3", "3")]),
        ("exit0-close", ["new-pane", "--close-on-exit", "--name", "exit0", "--",
                         *spike.py("agent.py", "exit0-close", "0")]),
        ("missing", ["new-pane", "--name", "missing", "--",
                     str(spike.root / "bin" / "does-not-exist")]),
        ("missing-close", ["new-pane", "--close-on-exit", "--name", "missing-close", "--",
                           str(spike.root / "bin" / "does-not-exist")]),
        ("not-executable", ["new-pane", "--name", "not-exec", "--",
                            str(spike.root / "bin" / "agent.py")]),
    )
    for label, arguments in cases:
        code, out, err = spike.action(session, *arguments)
        pane_id = returned_id(out)["pane_id"]
        time.sleep(1.5)
        pane = spike.pane_for(spike.panes(session), pane_id)
        result[label] = {
            "action_exit": code, "stdout_shape": returned_id(out)["raw_shape"],
            "stderr": spike.redact(err)[:200],
            "pane_present_after_exit": pane is not None,
            "pane_state": {key: pane.get(key) for key in (
                "exited", "exit_status", "is_held", "title", "terminal_command",
                "pane_command") if key in pane} if pane else None,
        }
        if pane:
            result[label]["pane_state"] = {
                key: spike.redact(value) if isinstance(value, str) else value
                for key, value in result[label]["pane_state"].items()}
    code, out, err = spike.action(session, "new-pane", "--block-until-exit", "--close-on-exit",
                                  "--", *spike.py("agent.py", "blocking", "5"))
    result["block_until_exit"] = {"action_exit": code, "stdout": spike.redact(out)[:80],
                                  "stderr": spike.redact(err)[:200]}
    return result


def mouse_click(client, column, row):
    for final in ("M", "m"):
        os.write(client.fd, f"\x1b[<0;{column};{row}{final}".encode())
        time.sleep(0.05)


def step_focus(spike):
    """Sidebar click by an interactive client with a watch client attached."""
    session = spike.main
    first = spike.main_client
    result = {}
    typed_target(first, spike.io, "warmup-c1")
    watcher = spike.client(["watch", session], label="watch")
    time.sleep(2)
    result["watch_client_alive"] = watcher.pid in spike.tracker.alive()
    result["list_clients_with_watch"] = spike.clients(session)
    mouse_click(first, 5, 5)
    time.sleep(0.7)
    after_click = spike.client_panes(session)
    result["after_sidebar_click"] = after_click
    result["click_focused_sidebar"] = after_click.get("1") == "terminal_0"
    target = spike.out_pane
    response = spike.sidebar_run("m-sidebar", ["zellij", "action", "focus-pane-id",
                                               f"terminal_{target}"])
    time.sleep(0.7)
    after = spike.client_panes(session)
    routed = typed_target(first, spike.io, "after-focus-c1")
    result["focus_from_sidebar"] = {
        "argv": ["zellij", "action", "focus-pane-id", "terminal_<id>"],
        "exit": response["code"], "stderr": spike.redact(response["stderr"])[:200],
        "target_pane": target, "list_clients_after": after,
        "clicking_client_moved": after.get("1") == f"terminal_{target}",
        "clicking_client_keystroke_routed_to": routed,
        "watch_client_rows": [cid for cid in after if cid != "1"],
    }
    watch_typing = typed_target(watcher, spike.io, "typed-by-watch")
    result["watch_client_input_routed_to"] = watch_typing
    mouse_click(first, 5, 5)
    time.sleep(0.5)
    spike.tracker.add(watcher.pid, "watch")
    return result


def step_resurrection(spike):
    """EXITED detection and whether resurrection re-runs commands."""
    result = {}
    name = spike.session_name("ser")
    env = spike.env_with(ZELLIJ_CONFIG_DIR=str(spike.root / "cfg-ser"))
    layout = spike.layout("ser", "r")
    creator = spike.client(["--config-dir", str(spike.root / "cfg-ser"), "--session", name,
                            "--new-session-with-layout", str(layout)], env)
    spike.wait_started("r-sidebar")
    spike.wait_started("r-main")
    wait_until(lambda: spike.client_panes(name), 10)
    time.sleep(3)
    code, out, err = spike.zj("kill-session", name, env=env)
    result["kill_session"] = {"exit": code, "stderr": spike.redact(err)[:200]}
    spike.tracker.add(creator.pid, "client")
    time.sleep(1.5)
    listing = spike.sessions_listing()
    result["listing_after_kill"] = {
        flag: [spike.redact(line) for line in value["lines"] if spike.token in line]
        for flag, value in listing.items()}
    result["exited_marker_present"] = any(
        "EXITED" in line for line in listing["-n"]["lines"] if name in line)
    result["short_listing_includes_exited"] = any(
        line.strip() == name for line in listing["-s"]["lines"])
    counts = {tag: len(starts(spike.io, f"r-{tag}")) for tag in ("sidebar", "main", "tongs")}
    code, out, err = spike.action(name, "list-clients", env=env)
    result["action_against_exited"] = {"exit": code, "stderr": spike.redact(err)[:200]}

    resumed = spike.client(["--config-dir", str(spike.root / "cfg-ser"), "attach", name], env)
    wait_until(lambda: spike.client_panes(name), 10)
    time.sleep(4)
    after = {tag: len(starts(spike.io, f"r-{tag}")) for tag in ("sidebar", "main", "tongs")}
    result["attach_without_force"] = {
        "argv": ["zellij", "attach", "<session>"],
        "commands_rerun": {tag: after[tag] > counts[tag] for tag in after},
        "focused": spike.client_panes(name),
    }
    resumed.type_line("")
    time.sleep(3)
    pressed = {tag: len(starts(spike.io, f"r-{tag}")) for tag in ("sidebar", "main", "tongs")}
    result["after_enter_in_focused_pane"] = {
        "commands_started": {tag: pressed[tag] > after[tag] for tag in pressed}}
    spike.tracker.add(resumed.pid, "client")
    spike.zj("kill-session", name, env=env)
    time.sleep(1.5)
    counts = {tag: len(starts(spike.io, f"r-{tag}")) for tag in ("sidebar", "main", "tongs")}
    code, out, err = spike.zj("--config-dir", str(spike.root / "cfg-ser"), "attach",
                              "--create-background", name, env=env, timeout=10)
    time.sleep(4)
    after = {tag: len(starts(spike.io, f"r-{tag}")) for tag in ("sidebar", "main", "tongs")}
    result["attach_create_background_on_exited"] = {
        "exit": code, "stderr": spike.redact(err)[:200],
        "running_after": any(line.strip() == name for line in spike.zj(
            "list-sessions", "-s", env=env)[1].splitlines()),
        "exited_marker_after": any("EXITED" in line for line in spike.zj(
            "list-sessions", "-n", env=env)[1].splitlines() if name in line),
        "commands_rerun": {tag: after[tag] > counts[tag] for tag in after},
    }
    spike.zj("kill-session", name, env=env)
    time.sleep(1.5)
    counts = {tag: len(starts(spike.io, f"r-{tag}")) for tag in ("sidebar", "main", "tongs")}
    forced = spike.client(["--config-dir", str(spike.root / "cfg-ser"), "attach",
                           "--force-run-commands", name], env)
    wait_until(lambda: spike.client_panes(name), 10)
    time.sleep(4)
    after = {tag: len(starts(spike.io, f"r-{tag}")) for tag in ("sidebar", "main", "tongs")}
    result["attach_force_run_commands"] = {
        "argv": ["zellij", "attach", "--force-run-commands", "<session>"],
        "commands_rerun": {tag: after[tag] > counts[tag] for tag in after}}
    spike.tracker.add(forced.pid, "client")
    spike.zj("kill-session", name, env=env)
    time.sleep(1)
    code, out, err = spike.zj("delete-session", name, env=env)
    result["delete_session"] = {"exit": code, "stderr": spike.redact(err)[:200]}
    result["listed_after_delete"] = name in spike.zj("list-sessions", "-s", env=env)[1]
    return result


def step_tmux(spike):
    """Launch the workspace client from inside tmux and capture terminal labels."""
    if not shutil.which("tmux"):
        return {"skipped": "tmux not installed"}
    sock = str(spike.root / "tmux" / "sock")
    name = spike.session_name("tmux")
    layout = spike.root / "layout-tmux.kdl"
    capture = spike.py("capture.py", "in-zellij")
    args = " ".join(json.dumps(item) for item in capture[1:])
    layout.write_text(
        "layout {\n tab {\n"
        f"  pane command={json.dumps(capture[0])} {{ args {args}; }}\n"
        " }\n}\n")
    inner = " ".join(shlex.quote(item) for item in (
        spike.zellij, "--config-dir", str(spike.root / "cfg"), "--session", name,
        "--new-session-with-layout", str(layout)))
    code, out, err = run(["tmux", "-S", sock, "-f", "/dev/null", "new-session", "-d",
                          "-s", "spike", "-x", str(CLIENT_COLUMNS), "-y", str(CLIENT_ROWS),
                          inner], env=spike.env)
    started = wait_until(lambda: (spike.io / "capture-in-zellij").exists(), 20)
    spike.tracker.sweep()
    result = {"tmux_start_exit": code, "tmux_stderr": spike.redact(err)[:200],
              "pane_captured": bool(started)}
    if started:
        result["zellij_pane_inside_tmux"] = json.loads(
            (spike.io / "capture-in-zellij").read_text())
    outside = spike.py("capture.py", "outside-new-pane")
    tmux_env = spike.env_with(TMUX=f"{sock},1,0", TMUX_PANE="%0")
    code, out, err = spike.action(name, "new-pane", "--", *outside, env=tmux_env)
    if wait_until(lambda: (spike.io / "capture-outside-new-pane").exists(), 10):
        result["new_pane_from_tmux_caller"] = json.loads(
            (spike.io / "capture-outside-new-pane").read_text())

    stripped = spike.session_name("tmux-clean")
    capture = spike.py("capture.py", "stripped")
    args = " ".join(json.dumps(item) for item in capture[1:])
    clean_layout = spike.root / "layout-tmux-clean.kdl"
    clean_layout.write_text(
        "layout {\n tab {\n"
        f"  pane command={json.dumps(capture[0])} {{ args {args}; }}\n"
        " }\n}\n")
    inner = " ".join(shlex.quote(item) for item in (
        "env", "-u", "TMUX", "-u", "TMUX_PANE", spike.zellij, "--config-dir",
        str(spike.root / "cfg"), "--session", stripped, "--new-session-with-layout",
        str(clean_layout)))
    code, out, err = run(["tmux", "-S", sock, "new-window", "-t", "spike", inner],
                         env=spike.env)
    result["clean_tmux_window_exit"] = code
    result["clean_tmux_stderr"] = spike.redact(err)[:200]
    if wait_until(lambda: (spike.io / "capture-stripped").exists(), 20):
        result["creator_env_stripped_of_tmux"] = json.loads(
            (spike.io / "capture-stripped").read_text())
    spike.tracker.sweep()
    spike.zj("kill-session", stripped)
    spike.zj("kill-session", name)
    return result


STEPS = (("create", step_create), ("spawn", step_spawn), ("listings", step_listings),
         ("exit", step_exit), ("focus", step_focus), ("resurrection", step_resurrection),
         ("tmux", step_tmux))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--zellij-dir", required=True, type=Path,
                        help="directory holding an extracted upstream zellij binary")
    parser.add_argument("--out", type=Path, help="write the redacted JSON record here")
    parser.add_argument("--no-write", action="store_true",
                        help="print the record only (the default when --out is absent)")
    parser.add_argument("--only", nargs="*", choices=[name for name, _ in STEPS])
    arguments = parser.parse_args(argv)
    if sys.platform != "linux":
        print("This spike requires Linux", file=sys.stderr)
        return 2
    zellij_dir = arguments.zellij_dir.resolve()
    if not (zellij_dir / "zellij").is_file():
        print("--zellij-dir must contain a zellij binary", file=sys.stderr)
        return 2
    token = "taspike-" + secrets.token_hex(4)
    before = config_snapshot()
    record = {"schema": SCHEMA}
    with tempfile.TemporaryDirectory(prefix=f"{token}-", dir="/tmp") as temporary:
        spike = Spike(Path(temporary), token, zellij_dir)
        record["zellij_version"] = spike.zj("--version")[1]
        steps = {}
        try:
            for name, step in STEPS:
                if arguments.only and name != "create" and name not in arguments.only:
                    continue
                print(f"step {name}", file=sys.stderr)
                try:
                    steps[name] = step(spike)
                except Exception as error:
                    steps[name] = {"error": spike.redact(f"{type(error).__name__}: {error}")}
                    if name == "create":
                        break
        finally:
            cleanup = spike.cleanup()
        stray = [pid for pid in Tracker([temporary, token]).stray()
                 if temporary in process_cmdline(pid) or token in process_cmdline(pid)]
        record["steps"] = steps
        redact = spike.redact
    record["run"] = {
        "platform": platform_facts(),
        "tested_commit": run(["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"])[1] or None,
        "cleanup": {**cleanup, "marker_strays_after_run": len(stray)},
        "user_config_unchanged": before == config_snapshot(),
        "user_zellij_cache_entries_for_run": zellij_cache_mentions(token),
    }
    text = redact(json.dumps(record, indent=2) + "\n")
    leaks = Redactor.for_host().leaks(text) + (["/home/"] if "/home/" in text else [])
    if leaks:
        print(f"refusing to emit record: leaks={leaks}", file=sys.stderr)
        return 1
    if arguments.out and not arguments.no_write:
        arguments.out.write_text(text)
    print(text)
    healthy = (cleanup["survivors"] == 0 and not stray and before == config_snapshot()
               and not any("error" in value for value in steps.values()))
    return 0 if healthy else 1


if __name__ == "__main__":
    sys.exit(main())
