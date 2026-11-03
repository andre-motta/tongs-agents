"""Subcommand modules for the ``tongs-agents`` CLI.

Every module in this package whose name does not start with ``_`` is one
subcommand. ``cli.main`` imports them with ``pkgutil`` in sorted order and adds
each as a subparser next to the built-in verbs (install, uninstall, monitor,
focus, status).

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

A module that lacks ``add_arguments`` or ``run``, or whose name collides with
a built-in verb or another module, makes ``cli.main`` raise
``CommandCollisionError`` (or ``CommandModuleError``) instead of shadowing it.
Modules import ``tongs_agents.cli`` freely: this package never imports it at
module level, so there is no import cycle.
"""

from __future__ import annotations

import importlib
import pkgutil
from types import ModuleType


class CommandModuleError(RuntimeError):
    """A command module that does not follow the contract above."""


class CommandCollisionError(CommandModuleError):
    """Two commands, or a module and a built-in verb, share a name."""


def command_name(module: ModuleType) -> str:
    name = getattr(module, "NAME", None)
    if name is None:
        name = module.__name__.rsplit(".", 1)[-1].replace("_", "-")
    if not isinstance(name, str) or not name:
        raise CommandModuleError(f"{module.__name__}: NAME must be a non-empty string")
    return name


def discover(package: ModuleType | None = None) -> list[ModuleType]:
    """Import every public module of ``package`` (this one by default), sorted."""
    if package is None:
        package = importlib.import_module(__name__)
    modules = []
    names = sorted(
        info.name for info in pkgutil.iter_modules(package.__path__)
        if not info.name.startswith("_")
    )
    for name in names:
        module = importlib.import_module(f"{package.__name__}.{name}")
        entries = getattr(module, "COMMANDS", None)
        for entry in (module,) if entries is None else entries:
            for attribute in ("add_arguments", "run"):
                if not callable(getattr(entry, attribute, None)):
                    raise CommandModuleError(f"{module.__name__}: missing {attribute}()")
            modules.append(entry)
    modules.sort(key=command_name)
    return modules
