import importlib
import sys
from types import ModuleType

import pytest

from tongs_agents import cli, commands


def _module(name, **attributes):
    module = ModuleType(f"fake_commands.{name}")
    module.add_arguments = lambda parser: None
    module.run = lambda args: 0
    for key, value in attributes.items():
        setattr(module, key, value)
    return module


@pytest.fixture
def package(tmp_path, monkeypatch, request):
    name = f"fake_cmds_{request.node.name.replace('[', '_').replace(']', '_')}"
    root = tmp_path / name
    root.mkdir()
    (root / "__init__.py").write_text("")
    monkeypatch.syspath_prepend(str(tmp_path))

    def make(files):
        for filename, text in files.items():
            (root / filename).write_text(text)
        importlib.invalidate_caches()
        return importlib.import_module(name)

    yield make
    for module in [key for key in sys.modules if key == name or key.startswith(f"{name}.")]:
        del sys.modules[module]


CONTRACT = "def add_arguments(parser):\n    pass\n\ndef run(args):\n    return 0\n"


def test_builtin_command_modules_are_discovered_sorted():
    names = [commands.command_name(module) for module in commands.discover()]
    assert names == sorted(names)
    assert {"doctor", "profiles"} <= set(names)


def test_discover_imports_public_modules_in_order(package):
    pkg = package({
        "zeta.py": CONTRACT,
        "alpha_beta.py": CONTRACT,
        "_private.py": "raise RuntimeError('must not import')\n",
    })
    modules = commands.discover(pkg)
    assert [commands.command_name(module) for module in modules] == ["alpha-beta", "zeta"]


def test_module_without_run_is_rejected(package):
    pkg = package({"broken.py": "def add_arguments(parser):\n    pass\n"})
    with pytest.raises(commands.CommandModuleError, match="missing run"):
        commands.discover(pkg)


@pytest.mark.parametrize("name", sorted(cli.BUILTIN_COMMANDS))
def test_collision_with_builtin_verb_raises(name):
    with pytest.raises(commands.CommandCollisionError, match=repr(name)):
        cli.build_parser([_module("x", NAME=name)])


def test_collision_between_modules_raises():
    with pytest.raises(commands.CommandCollisionError):
        cli.build_parser([_module("a", NAME="same"), _module("b", NAME="same")])


def test_invalid_name_is_rejected():
    with pytest.raises(commands.CommandModuleError):
        cli.build_parser([_module("x", NAME="")])


def test_module_command_is_dispatched(monkeypatch):
    seen = []

    def add_arguments(parser):
        parser.add_argument("--flag", action="store_true")

    module = _module("hello", NAME="hello", add_arguments=add_arguments,
                     run=lambda args: seen.append(args.flag) or 3)
    monkeypatch.setattr(cli.commands, "discover", lambda: [module])
    monkeypatch.setattr(sys, "argv", ["tongs-agents", "hello", "--flag"])
    with pytest.raises(SystemExit) as raised:
        cli.main()
    assert raised.value.code == 3
    assert seen == [True]


def test_zero_status_does_not_exit(monkeypatch):
    module = _module("quiet", NAME="quiet", run=lambda args: None)
    monkeypatch.setattr(cli.commands, "discover", lambda: [module])
    monkeypatch.setattr(sys, "argv", ["tongs-agents", "quiet"])
    cli.main()
