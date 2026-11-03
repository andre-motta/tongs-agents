"""Subcommand modules for the ``tongs-agents`` CLI.

Every module in this package whose name does not start with ``_`` is one
subcommand. ``cli.main`` finds them with ``pkgutil`` and adds each as a
subparser next to the built-in verbs (install, uninstall, monitor, focus,
status). ``discover`` returns the commands sorted by command name, not by
module file name, so the entries of a ``COMMANDS`` module interleave with the
others in ``tongs-agents --help``. This order is part of the contract (#90).

A command module defines:

``NAME``
    The subcommand name. Optional; defaults to the module name with ``_``
    replaced by ``-``.
``HELP``
    One line shown by ``tongs-agents --help``.
``add_arguments(parser)``
    Adds the subcommand's options to its ``argparse.ArgumentParser``.
``run(args)``
    Runs the subcommand and returns its exit status (``None`` means 0). It may
    raise ``cli.ConfigError`` for a config file it refuses to use.

and may define:

``doctor_checks()``
    Returns an iterable of ``commands.doctor.Check`` that
    ``tongs-agents doctor`` reports with its built-in checks. It must only
    read: no writes, no network and no long-running subprocess.

A module may instead define ``COMMANDS``, a sequence of objects that each
follow the contract above (``NAME`` required), to add several subcommands.

Command modules are imported lazily (#120). ``discover`` reads ``NAME`` and
``HELP`` from the module source without importing it when both are literal
top-level assignments placed before the first function or class, and the
module defines ``add_arguments`` and ``run`` with ``def`` at the top level.
Such a module is imported only when its verb is parsed or run, or when
``doctor`` asks for its checks, so ``tongs-agents --help``, ``status`` and
``notify`` never import textual or the sandbox wizard, and a module that fails
to import breaks only its own verb. Any other module (``COMMANDS``, computed
metadata) is imported by ``discover``; if that import fails, its verb is still
listed under the module's default name and fails when used.

Usage errors exit 1 (``EXIT_USAGE``), never argparse's 2, which several verbs
use for their own outcomes (``wait`` timeout, ``web`` and ``zellij``
unsupported, ``doctor`` errors). ``cli.build_parser`` uses ``CommandParser``
so every verb and nested subparser inherits that.

A module that lacks ``add_arguments`` or ``run``, or whose name collides with
a built-in verb or another module, makes ``cli.main`` raise
``CommandCollisionError`` (or ``CommandModuleError``) instead of shadowing it.
Modules import ``tongs_agents.cli`` freely: this package never imports it at
module level, so there is no import cycle.
"""

from __future__ import annotations

import argparse
import ast
import importlib
import importlib.util
import pkgutil
import re
from types import ModuleType

EXIT_USAGE = 1
METADATA = ("NAME", "HELP")
_BODY = re.compile(r"^(?:def |async def |class |@)", re.MULTILINE)
_REBOUND = re.compile(r"^(?:NAME|HELP|COMMANDS)\b", re.MULTILINE)
_STAR_IMPORT = re.compile(r"^from\s+\S+\s+import\s+\*", re.MULTILINE)
_REQUIRED = tuple(
    re.compile(rf"^def {name}\(", re.MULTILINE) for name in ("add_arguments", "run")
)


class CommandModuleError(RuntimeError):
    """A command module that does not follow the contract above."""


class CommandCollisionError(CommandModuleError):
    """Two commands, or a module and a built-in verb, share a name."""


class CommandParser(argparse.ArgumentParser):
    """An argument parser whose usage errors exit 1 and whose options load on first use."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._pending = None

    def defer(self, add_arguments) -> None:
        """Run ``add_arguments(self)`` only when this parser is first used."""
        self._pending = add_arguments

    def _load_arguments(self) -> None:
        pending, self._pending = self._pending, None
        if pending is not None:
            pending(self)

    def parse_known_args(self, args=None, namespace=None):
        self._load_arguments()
        return super().parse_known_args(args, namespace)

    def format_usage(self):
        self._load_arguments()
        return super().format_usage()

    def format_help(self):
        self._load_arguments()
        return super().format_help()

    def error(self, message):
        try:
            super().error(message)
        except SystemExit:
            raise SystemExit(EXIT_USAGE) from None


class LazyCommand:
    """A command module that is imported only when it is used."""

    def __init__(self, module_name: str, metadata: dict):
        self.__name__ = module_name
        self.NAME = metadata.get("NAME")
        self.HELP = metadata.get("HELP")
        self._module = None

    def load(self) -> ModuleType:
        if self._module is None:
            module = importlib.import_module(self.__name__)
            if getattr(module, "COMMANDS", None) is not None:
                raise CommandModuleError(f"{module.__name__}: COMMANDS cannot be loaded lazily")
            _check_contract(module, module)
            self._module = module
        return self._module

    def add_arguments(self, parser) -> None:
        if isinstance(parser, CommandParser):
            parser.defer(lambda target: self.load().add_arguments(target))
        else:
            self.load().add_arguments(parser)

    def run(self, args):
        return self.load().run(args)

    def __getattr__(self, attribute):
        if attribute.startswith("__") or attribute == "_module":
            raise AttributeError(attribute)
        try:
            module = self.load()
        except Exception as error:
            if attribute == "doctor_checks":
                # doctor reports a module that cannot load instead of crashing with it.
                return _failing(error)
            raise AttributeError(f"{self.__name__} cannot be loaded: {error}") from error
        return getattr(module, attribute)


def _failing(error: Exception):
    def provider():
        raise error
    return provider


def _check_contract(module: ModuleType, entry) -> None:
    for attribute in ("add_arguments", "run"):
        if not callable(getattr(entry, attribute, None)):
            raise CommandModuleError(f"{module.__name__}: missing {attribute}()")


def command_name(module: ModuleType) -> str:
    name = getattr(module, "NAME", None)
    if name is None:
        name = module.__name__.rsplit(".", 1)[-1].replace("_", "-")
    if not isinstance(name, str) or not name:
        raise CommandModuleError(f"{module.__name__}: NAME must be a non-empty string")
    return name


def static_metadata(module_name: str) -> dict | None:
    """``NAME`` and ``HELP`` read from the module source, or None to import it."""
    try:
        spec = importlib.util.find_spec(module_name)
        origin = spec.origin if spec is not None else None
        if not origin or not origin.endswith(".py"):
            return None
        with open(origin, encoding="utf-8") as stream:
            source = stream.read()
    except (ImportError, OSError, UnicodeError, ValueError):
        return None
    if _STAR_IMPORT.search(source) or not all(pattern.search(source) for pattern in _REQUIRED):
        return None
    body = _BODY.search(source)
    head = source if body is None else source[:body.start()]
    if body is not None and _REBOUND.search(source, body.start()):
        return None
    try:
        tree = ast.parse(head)
    except SyntaxError:
        return None
    metadata = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets, value = [node.target], node.value
        else:
            continue
        for target in targets:
            names = {item.id for item in ast.walk(target) if isinstance(item, ast.Name)}
            if "COMMANDS" in names:
                return None
            if not names & set(METADATA):
                continue
            if not isinstance(target, ast.Name) or not isinstance(node, ast.Assign):
                return None
            try:
                metadata[target.id] = ast.literal_eval(value)
            except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
                return None
    return metadata


def discover(package: ModuleType | None = None) -> list:
    """Every public command of ``package`` (this one by default), sorted by name.

    Modules whose metadata reads statically come back as ``LazyCommand``;
    the rest are imported here.
    """
    if package is None:
        package = importlib.import_module(__name__)
    modules = []
    names = sorted(
        info.name for info in pkgutil.iter_modules(package.__path__)
        if not info.name.startswith("_")
    )
    for name in names:
        full_name = f"{package.__name__}.{name}"
        metadata = static_metadata(full_name)
        if metadata is not None:
            modules.append(LazyCommand(full_name, metadata))
            continue
        try:
            module = importlib.import_module(full_name)
        except Exception:  # noqa: BLE001 - one broken module must not hide the other verbs
            modules.append(LazyCommand(full_name, {}))
            continue
        entries = getattr(module, "COMMANDS", None)
        for entry in (module,) if entries is None else entries:
            _check_contract(module, entry)
            modules.append(entry)
    modules.sort(key=command_name)
    return modules
