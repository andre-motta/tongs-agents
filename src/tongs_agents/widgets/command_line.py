"""The sidebar command line: ``:`` opens a one-line prompt for workspace verbs.

A line is split like a shell line (``shlex.split``) but never run through a
shell: its first word must name a registered verb, and the rest is parsed by
that verb's own argument parser, built from the same ``add_arguments`` as the
CLI verb, so the syntax, the checks and the messages are those of the CLI.
For a passthrough verb such as ``run``, everything after the first ``--`` is
the harness's and argparse never sees it, exactly as ``cli.parse_args``
splits it. Anything else, such as ``ls`` or ``!make``, is refused.

The registry holds ``run`` (``commands.run``: ``run PROFILE [DIR] [-- ARGS]``
in the sidebar's own session). A verb is a ``Verb``: its name, its
``add_arguments``, the completion kind of each positional and ``prepare``,
which checks the parsed arguments on the UI thread and returns a ``Prepared``
whose ``execute`` runs in a worker thread. ``register_verb`` adds one.

Up and Down walk the history of submitted lines, kept in memory for the life
of the sidebar. Tab completes the verb, a profile name or a folder (relative
to the folder the sidebar started in, ``~`` expanded; hidden folders only
after a typed dot) and lists the candidates when several remain. Escape
closes the prompt. The result or the error is shown under the prompt.
"""

from __future__ import annotations

import argparse
import os
import shlex
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Input, Static

from tongs_agents import workspace_spawn
from tongs_agents.commands import run as run_command
from tongs_agents.launches import LaunchError
from tongs_agents.multiplexers import Multiplexer, MultiplexerError
from tongs_agents.profiles import Profile, ProfileError
from tongs_agents.widgets.add_agent import MAX_COMPLETION_ENTRIES

PASSTHROUGH = "--"
HISTORY_LIMIT = 100
MAX_LISTED_CANDIDATES = 30
COMPLETE_PROFILE = "profile"
COMPLETE_FOLDER = "folder"


class CommandError(Exception):
    """A line that was refused or a command that failed; shown under the prompt."""


class CommandHelp(Exception):  # noqa: N818 - control flow for -h, not an error
    """``-h``: the verb's help text, shown under the prompt instead of printed."""


@dataclass(frozen=True)
class CommandContext:
    """Where commands run: the sidebar's multiplexer and session, and its profiles."""

    multiplexer: Multiplexer
    session: str
    load_profiles: Callable[[], Mapping[str, Profile]]


@dataclass(frozen=True)
class Prepared:
    """A checked command: ``summary`` while it runs, ``execute`` in a worker thread.

    ``execute`` returns the result line or raises CommandError.
    """

    summary: str
    execute: Callable[[], str]


@dataclass(frozen=True)
class Verb:
    """One workspace verb of the command line.

    ``completions`` names the completion kind of each positional in order
    (``COMPLETE_PROFILE``, ``COMPLETE_FOLDER`` or ``""`` for none).
    ``passthrough`` hands everything after ``--`` to ``args.extra``.
    """

    name: str
    help: str
    add_arguments: Callable[[argparse.ArgumentParser], None]
    prepare: Callable[[CommandContext, argparse.Namespace], Prepared]
    completions: tuple[str, ...] = ()
    passthrough: bool = False


class VerbParser(argparse.ArgumentParser):
    """An argument parser that raises instead of printing and exiting."""

    def error(self, message):
        raise CommandError(f"{self.prog}: {message}\n{self.format_usage().strip()}")

    def exit(self, status=0, message=None):
        raise CommandError((message or "").strip() or f"{self.prog}: exit {status}")

    def print_help(self, file=None):
        raise CommandHelp(self.format_help().strip())

    def print_usage(self, file=None):
        raise CommandHelp(self.format_usage().strip())


def build_parser(verb: Verb) -> VerbParser:
    parser = VerbParser(prog=verb.name, description=verb.help)
    if hasattr(parser, "color"):
        # Python 3.14 colours help for a terminal; the text goes into a widget.
        parser.color = False
    verb.add_arguments(parser)
    return parser


VERBS: dict[str, Verb] = {}


def register_verb(verb: Verb) -> Verb:
    """Add ``verb`` to the command line; a name is registered once."""
    if verb.name in VERBS:
        raise ValueError(f"verb {verb.name!r} is already registered")
    VERBS[verb.name] = verb
    return verb


def parse_command(text: str, verbs: Mapping[str, Verb]) -> tuple[Verb, argparse.Namespace]:
    """The verb and its parsed arguments; CommandError or CommandHelp otherwise."""
    try:
        words = shlex.split(text)
    except ValueError as error:
        raise CommandError(f"cannot split the line: {error}") from None
    if not words:
        raise CommandError("type a command, for example: run PROFILE [DIR] [-- ARGS]")
    name, rest = words[0], words[1:]
    verb = verbs.get(name)
    if verb is None:
        known = ", ".join(sorted(verbs)) or "none"
        raise CommandError(
            f"unknown command {name!r:.40}; commands: {known} (shell commands are not run)"
        )
    tail: list[str] = []
    if verb.passthrough and PASSTHROUGH in rest:
        index = rest.index(PASSTHROUGH)
        rest, tail = rest[:index], rest[index + 1:]
    args = build_parser(verb).parse_args(rest)
    if verb.passthrough:
        args.extra = tail
    return verb, args


# -- Completion --

def folder_candidates(word: str) -> list[str]:
    """The directories ``word`` can extend to, each ending with a separator.

    Relative words are read against the current directory. Hidden directories
    are offered only once the typed name starts with a dot. At most
    ``MAX_COMPLETION_ENTRIES`` entries are read.
    """
    if word == "~":
        return ["~" + os.sep]
    if word.startswith("~") and os.sep not in word:
        return []
    directory, prefix = os.path.split(os.path.expanduser(word))
    lead = word[:len(word) - len(prefix)]
    names = []
    try:
        with os.scandir(directory or os.curdir) as entries:
            for index, entry in enumerate(entries):
                if index >= MAX_COMPLETION_ENTRIES:
                    break
                name = entry.name
                if not name.startswith(prefix):
                    continue
                if name.startswith(".") and not prefix.startswith("."):
                    continue
                try:
                    if entry.is_dir():
                        names.append(name)
                except OSError:
                    continue
    except OSError:
        return []
    return sorted(lead + name + os.sep for name in names)


def _value_options(verb: Verb) -> set[str]:
    """The verb's option strings that take a value (``--session NAME``)."""
    parser = build_parser(verb)
    return {
        option for option, action in parser._option_string_actions.items()
        if action.nargs != 0
    }


def _positional_kind(verb: Verb, previous: list[str]) -> str | None:
    """The completion kind of the word after ``previous`` (the verb's arguments)."""
    if verb.passthrough and PASSTHROUGH in previous:
        return None
    takes_value = _value_options(verb)
    count = 0
    skip = False
    for word in previous:
        if skip:
            skip = False
        elif word.startswith("-") and word != "-":
            skip = "=" not in word and word in takes_value
        else:
            count += 1
    if skip or count >= len(verb.completions):
        return None
    return verb.completions[count] or None


def _shown(candidate: str) -> str:
    stripped = candidate.rstrip(os.sep)
    if stripped == candidate:
        return candidate
    return os.path.basename(stripped) + os.sep


def _quoted(word: str) -> str:
    """``word`` quoted for the line, a leading ``~`` or ``~/`` left bare.

    ``shlex.split`` never expands ``~`` but ``shlex.quote`` quotes it, so a
    bare tilde keeps the word readable and completable (``~/'my dir/'``).
    """
    if word.startswith("~"):
        cut = word.find(os.sep) + 1
        if cut and shlex.quote(word[1:cut]) == word[1:cut]:
            rest = word[cut:]
            return word[:cut] + (shlex.quote(rest) if rest else "")
    return shlex.quote(word)


def _split_last(value: str) -> tuple[str, list[str], str] | None:
    """The raw text before the last word, the words before it and the last word.

    Words are unquoted as ``shlex.split`` reads them. The last word is empty
    when the line ends with a separator. ``None`` for a line ``shlex`` cannot
    split, such as one with an unclosed quote.
    """
    try:
        words = shlex.split(value)
        sentinel = shlex.split(value + "x")
    except ValueError:
        return None
    if len(sentinel) == len(words) + 1:
        return value, words, ""
    previous, last = words[:-1], words[-1]
    for cut in range(len(value) - 1, -1, -1):
        if not value[cut].isspace():
            continue
        try:
            if (shlex.split(value[cut + 1:]) == [last]
                    and shlex.split(value[:cut + 1]) == previous):
                return value[:cut + 1], previous, last
        except ValueError:
            continue
    return None if previous else ("", previous, last)


def complete_line(
    value: str, verbs: Mapping[str, Verb], profile_names: Callable[[], list[str]],
) -> tuple[str, list[str]]:
    """``value`` with its last word completed, and the candidates to list.

    Only the last word is completed. It is read unquoted, as ``shlex.split``
    reads it, and written back quoted where needed; a line with an unclosed
    quote is left alone.
    """
    split = _split_last(value)
    if split is None:
        return value, []
    head, previous, word = split
    if not previous:
        candidates = sorted(name for name in verbs if name.startswith(word))
        kind = "verb"
    else:
        verb = verbs.get(previous[0])
        if verb is None or word.startswith("-"):
            return value, []
        kind = _positional_kind(verb, previous[1:])
        if kind == COMPLETE_PROFILE:
            candidates = [name for name in profile_names() if name.startswith(word)]
        elif kind == COMPLETE_FOLDER:
            candidates = folder_candidates(word)
        else:
            return value, []
    if not candidates:
        return value, []
    if len(candidates) == 1:
        (only,) = candidates
        ending = "" if kind == COMPLETE_FOLDER else " "
        return head + _quoted(only) + ending, []
    common = os.path.commonprefix(candidates)
    shown = [_shown(candidate) for candidate in candidates]
    if len(common) > len(word):
        return head + _quoted(common), shown
    return value, shown


# -- The run verb --

def _prepare_run(context: CommandContext, args: argparse.Namespace) -> Prepared:
    mux, session = context.multiplexer, context.session
    try:
        run_command.refuse_options(mux.name, args, session)
        try:
            profiles = context.load_profiles()
        except ProfileError as error:
            raise CommandError(str(error)) from None
        profile, options = run_command.prepare(args, profiles, prog=run_command.NAME)
    except run_command.RunRefused as refused:
        raise CommandError(str(refused)) from None
    text = run_command.described(profile, options)
    if options["args"]:
        text += f" with {shlex.join(options['args'])}"

    def execute() -> str:
        try:
            workspace_spawn.spawn_profiles(mux, session, [profile], **options)
        except (MultiplexerError, LaunchError, OSError) as error:
            raise CommandError(getattr(error, "detail", None) or str(error)) from None
        return f"started {text}"

    return Prepared(f"starting {text}", execute)


RUN = register_verb(Verb(
    name=run_command.NAME,
    help=run_command.HELP,
    add_arguments=run_command.add_arguments,
    prepare=_prepare_run,
    completions=(COMPLETE_PROFILE, COMPLETE_FOLDER),
    passthrough=True,
))


# -- The widget --

class CommandLine(Widget):
    """The prompt and its result line; hidden until ``open``."""

    DEFAULT_CSS = """
    CommandLine {
        height: auto;
        display: none;
        layout: vertical;
    }
    CommandLine.visible {
        display: block;
    }
    CommandLine > Horizontal {
        height: 1;
    }
    CommandLine .command-prompt {
        width: 2;
        padding: 0 0 0 1;
        color: $accent;
    }
    CommandLine Input {
        border: none;
        height: 1;
        padding: 0;
        width: 1fr;
    }
    CommandLine > .command-result {
        height: auto;
        max-height: 16;
        padding: 0 1;
    }
    CommandLine > .command-result.error {
        color: $error;
    }
    """

    BINDINGS = [
        Binding("up", "history(-1)", "Older", show=False),
        Binding("down", "history(1)", "Newer", show=False),
        Binding("tab", "complete", "Complete", show=False),
        Binding("escape", "close", "Close", show=False),
    ]

    class Ran(Message):
        """A command finished; ``ok`` is False when it failed."""

        def __init__(self, line: str, ok: bool, result: str) -> None:
            super().__init__()
            self.line = line
            self.ok = ok
            self.result = result

    class Closed(Message):
        """The prompt was closed with Escape."""

    def __init__(self, context: CommandContext, *, verbs: Mapping[str, Verb] | None = None,
                 name: str | None = None, id: str | None = None,
                 classes: str | None = None) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self.context = context
        self.verbs = VERBS if verbs is None else verbs
        self.history: list[str] = []
        self._history_index: int | None = None
        self._draft = ""

    def compose(self) -> ComposeResult:
        with Horizontal():
            yield Static(":", classes="command-prompt")
            yield Input(placeholder="run PROFILE [DIR] [-- ARGS]", id="command-input")
        yield Static("", classes="command-result")

    @property
    def input(self) -> Input:
        return self.query_one("#command-input", Input)

    @property
    def result_text(self) -> str:
        return str(self.query_one(".command-result", Static).render())

    def show_result(self, message: str, *, error: bool = False) -> None:
        result = self.query_one(".command-result", Static)
        result.set_class(error, "error")
        result.update(Text(message))

    def open(self) -> None:
        self.add_class("visible")
        self.input.focus()

    def action_close(self) -> None:
        self.remove_class("visible")
        self.input.value = ""
        self._history_index = None
        self.show_result("")
        self.post_message(self.Closed())

    # -- History --

    def _remember(self, line: str) -> None:
        if not self.history or self.history[-1] != line:
            self.history.append(line)
            del self.history[:-HISTORY_LIMIT]
        self._history_index = None
        self._draft = ""

    def _set_value(self, value: str) -> None:
        field = self.input
        field.value = value
        field.cursor_position = len(value)

    def action_history(self, step: int) -> None:
        if not self.history:
            return
        index = self._history_index
        if index is None:
            if step > 0:
                return
            self._draft = self.input.value
            index = len(self.history) - 1
        else:
            index = max(0, index + step)
            if index >= len(self.history):
                self._history_index = None
                self._set_value(self._draft)
                return
        self._history_index = index
        self._set_value(self.history[index])

    # -- Completion --

    def _profile_names(self) -> list[str]:
        try:
            return sorted(self.context.load_profiles())
        except ProfileError:
            return []

    def action_complete(self) -> None:
        field = self.input
        if field.cursor_position != len(field.value):
            return
        value, candidates = complete_line(field.value, self.verbs, self._profile_names)
        if value != field.value:
            self._set_value(value)
        if candidates:
            listed = "  ".join(candidates[:MAX_LISTED_CANDIDATES])
            if len(candidates) > MAX_LISTED_CANDIDATES:
                listed += f"  (+{len(candidates) - MAX_LISTED_CANDIDATES} more)"
            self.show_result(listed)

    # -- Running --

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        line = event.value.strip()
        if not line:
            return
        self._remember(line)
        try:
            verb, args = parse_command(line, self.verbs)
            prepared = verb.prepare(self.context, args)
        except CommandHelp as shown:
            self.input.value = ""
            self.show_result(str(shown))
            return
        except CommandError as error:
            self.show_result(str(error), error=True)
            return
        self.input.value = ""
        self.show_result(f"{prepared.summary}…")
        self._execute(line, prepared)

    @work(thread=True, group="command-line")
    def _execute(self, line: str, prepared: Prepared) -> None:
        try:
            ok, message = True, prepared.execute()
        except CommandError as error:
            ok, message = False, str(error)
        except Exception as error:  # noqa: BLE001 - report, never crash the sidebar
            ok, message = False, f"{type(error).__name__}: {error}"
        self.app.call_from_thread(self._finished, line, ok, message)

    def _finished(self, line: str, ok: bool, message: str) -> None:
        self.show_result(message, error=not ok)
        self.post_message(self.Ran(line, ok, message))
