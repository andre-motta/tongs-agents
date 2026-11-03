"""Fake agent REPL for the send validator; never a real agent.

A profile command runs it in a launcher pane: ``python fake_repl.py <io_dir>``.
Like an agent's prompt it puts its terminal in raw mode and turns on
bracketed paste, so pasted text (between ``ESC [200~`` and ``ESC [201~``) is
prompt content and only a carriage return typed outside a paste submits.

Each submitted prompt is appended as one JSON line to
``<io_dir>/repl-<profile>.jsonl`` with its number, text, byte count and
SHA-256, and acknowledged on screen as ``received #<n>: <first line>`` so a
screen capture can be checked. ``<io_dir>/repl-<profile>.ready`` appears once
bracketed paste is on. Ctrl-C or end of input exits. Only the standard library
is used and nothing is sent anywhere.
"""

import hashlib
import json
import os
from pathlib import Path
import re
import sys
import termios
import tty

PASTE_START = b"\x1b[200~"
PASTE_END = b"\x1b[201~"
ENABLE_PASTE = b"\x1b[?2004h"
DISABLE_PASTE = b"\x1b[?2004l"
PROMPT = b"fake-repl> "
SUBMIT = (b"\r", b"\n")
INTERRUPT = b"\x03"
SAFE_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}\Z")
SHOWN = 60


def profile_name(env):
    name = env.get("TONGS_AGENTS_PROFILE", "")
    return name if SAFE_NAME.match(name) else "unknown"


class Repl:
    """Bracketed-paste input parser that counts submitted prompts."""

    def __init__(self, record):
        self.record = record
        self.pending = b""
        self.current = bytearray()
        self.in_paste = False
        self.pastes = 0
        self.count = 0

    def feed(self, data):
        """Consume input bytes; return False once Ctrl-C was typed."""
        self.pending += data
        while self.pending:
            if self.in_paste:
                end = self.pending.find(PASTE_END)
                if end < 0:
                    keep = _partial_suffix(self.pending, PASTE_END)
                    self.current += self.pending[:len(self.pending) - keep]
                    self.pending = self.pending[len(self.pending) - keep:]
                    return True
                self.current += self.pending[:end]
                self.pending = self.pending[end + len(PASTE_END):]
                self.in_paste = False
                continue
            if self.pending.startswith(PASTE_START):
                self.pending = self.pending[len(PASTE_START):]
                self.in_paste = True
                self.pastes += 1
                continue
            if PASTE_START.startswith(self.pending):
                return True
            byte = self.pending[:1]
            self.pending = self.pending[1:]
            if byte == INTERRUPT:
                return False
            if byte in SUBMIT:
                self.submit()
            else:
                self.current += byte
        return True

    def submit(self):
        self.count += 1
        text = bytes(self.current).decode("utf-8", errors="replace")
        self.record({
            "n": self.count,
            "text": text,
            "bytes": len(self.current),
            "lines": len(text.split("\n")),
            "pastes": self.pastes,
            "sha256": hashlib.sha256(bytes(self.current)).hexdigest(),
        })
        self.current = bytearray()
        self.pastes = 0
        return text


def _partial_suffix(data, marker):
    """Length of the longest suffix of data that starts marker."""
    for size in range(min(len(data), len(marker) - 1), 0, -1):
        if marker.startswith(data[-size:]):
            return size
    return 0


def _write(data):
    os.write(sys.stdout.fileno(), data)


def main(argv):
    if len(argv) != 2:
        print("usage: fake_repl.py <io_dir>", file=sys.stderr)
        return 64
    io_dir = Path(argv[1])
    name = profile_name(os.environ)
    log = io_dir / f"repl-{name}.jsonl"

    def record(entry):
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")
        first = entry["text"].split("\n", 1)[0][:SHOWN]
        _write(f"\r\nreceived #{entry['n']}: {first}\r\n".encode() + PROMPT)

    stdin = sys.stdin.fileno()
    saved = termios.tcgetattr(stdin) if os.isatty(stdin) else None
    if saved is not None:
        tty.setraw(stdin)
    repl = Repl(record)
    try:
        _write(ENABLE_PASTE + b"fake-repl ready\r\n" + PROMPT)
        ready = io_dir / f"repl-{name}.ready"
        ready.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
        while True:
            data = os.read(stdin, 65536)
            if not data or not repl.feed(data):
                break
    finally:
        _write(DISABLE_PASTE)
        if saved is not None:
            termios.tcsetattr(stdin, termios.TCSADRAIN, saved)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
