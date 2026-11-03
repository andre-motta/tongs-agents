"""Command modules load lazily: the CLI's import budget and failure isolation (#120)."""

import importlib
import os
import subprocess
import sys

import pytest

from tongs_agents import cli, commands

HEAVY = (
    "textual",
    "sqlite3",
    "urllib.request",
    "tongs_agents.views.sandbox_wizard",
    "tongs_agents.chat",
    "tongs_agents.sandbox.setup",
    "tongs_agents.sandbox.approvals",
    "tongs_agents.tui",
)
CONTRACT = "def add_arguments(parser):\n    parser.add_argument('--flag', action='store_true')\n"
CONTRACT += "\ndef run(args):\n    return 7 if args.flag else 0\n"


def _modules_after(tmp_path, argv, *, run=False):
    """Modules loaded by ``tongs-agents ARGV`` in a fresh interpreter."""
    code = (
        "import contextlib, io, sys\n"
        "from tongs_agents import cli\n"
        f"sys.argv = ['tongs-agents', *{list(argv)!r}]\n"
        "parser, handlers = cli.build_parser()\n"
        "with contextlib.redirect_stdout(io.StringIO()), "
        "contextlib.redirect_stderr(io.StringIO()):\n"
        "    try:\n"
        "        args = parser.parse_args(sys.argv[1:])\n"
        f"        if {run!r}:\n"
        "            handlers[args.command](args)\n"
        "    except SystemExit:\n"
        "        pass\n"
        "print('\\n'.join(sorted(sys.modules)))\n"
    )
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith(("TONGS_AGENTS_", "ZELLIJ", "TMUX"))
    }
    env.update({
        "HOME": str(home),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "TONGS_AGENTS_STATE_DIR": str(tmp_path / "state" / "tongs-agents"),
        "TONGS_AGENTS_CONFIG_DIR": str(tmp_path / "config" / "tongs-agents"),
    })
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
        check=True, env=env,
    )
    return set(result.stdout.split())


@pytest.mark.parametrize("argv", [
    ["--help"],
    ["status", "--help"],
    ["uninstall", "--help"],
    ["uninstall", "--keep-data"],
    ["notify", "--help"],
    ["notify", "done", "all", "good"],
])
def test_light_verbs_do_not_import_heavy_command_modules(tmp_path, argv):
    modules = _modules_after(tmp_path, argv)
    assert not [name for name in HEAVY if name in modules]
    for name in ("chat", "sandbox", "sidebar", "web", "workspace"):
        assert f"tongs_agents.commands.{name}" not in modules


def test_running_notify_outside_a_pane_stays_light(tmp_path):
    modules = _modules_after(tmp_path, ["notify", "done", "x"], run=True)
    assert "tongs_agents.commands.notify" in modules
    assert not [name for name in HEAVY if name in modules]


def test_the_selected_heavy_verb_is_the_only_one_loaded(tmp_path):
    modules = _modules_after(tmp_path, ["chat", "--help"])
    assert "tongs_agents.commands.chat" in modules
    assert "tongs_agents.commands.sandbox" not in modules
    assert "tongs_agents.views.sandbox_wizard" not in modules


def test_every_verb_is_still_registered():
    parser, handlers = cli.build_parser()
    expected = {
        "agent-status", "chat", "cleanup", "config", "doctor", "notify", "profiles", "read", "run",
        "sandbox", "send", "sidebar", "wait", "web", "workspace", "zellij",
    }
    assert expected | set(cli.BUILTIN_COMMANDS) == set(handlers)
    help_text = parser.format_help()
    for module in commands.discover():
        assert commands.command_name(module) in help_text


def test_static_metadata_matches_the_imported_modules():
    lazy = [entry for entry in commands.discover() if isinstance(entry, commands.LazyCommand)]
    assert {entry.__name__.rsplit(".", 1)[-1] for entry in lazy} >= {
        "chat", "sandbox", "sidebar", "notify", "web", "workspace",
    }
    for entry in lazy:
        module = importlib.import_module(entry.__name__)
        assert entry.NAME == getattr(module, "NAME", None)
        assert entry.HELP == getattr(module, "HELP", None)


def test_deferred_arguments_load_when_the_verb_is_parsed():
    loaded = []
    lazy = commands.LazyCommand("fake.verb", {"NAME": "verb", "HELP": "A verb"})
    lazy._module = type("Module", (), {
        "add_arguments": staticmethod(lambda parser: loaded.append(parser)
                                      or parser.add_argument("--flag", action="store_true")),
        "run": staticmethod(lambda args: 7 if args.flag else 0),
    })
    parser, handlers = cli.build_parser([lazy])
    assert loaded == []
    args = parser.parse_args(["verb", "--flag"])
    assert len(loaded) == 1
    assert handlers["verb"](args) == 7
    parser.parse_args(["verb"])
    assert len(loaded) == 1


@pytest.fixture
def package(tmp_path, monkeypatch, request):
    name = f"fake_lazy_{request.node.name.replace('[', '_').replace(']', '_')}"
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


def test_a_broken_module_breaks_only_its_own_verb(package, monkeypatch):
    pkg = package({
        "good.py": "NAME = 'good'\nHELP = 'Works'\n" + CONTRACT,
        "broken.py": (
            "NAME = 'broken'\nHELP = (\n    'Imports a missing widget'\n)\n"
            "import no_such_module_anywhere\n" + CONTRACT
            + "\ndef doctor_checks():\n    return []\n"
        ),
    })
    found = commands.discover(pkg)
    assert [commands.command_name(entry) for entry in found] == ["broken", "good"]
    assert all(isinstance(entry, commands.LazyCommand) for entry in found)
    assert f"{pkg.__name__}.broken" not in sys.modules
    parser, handlers = cli.build_parser(found)
    assert "Imports a missing widget" in parser.format_help()
    args = parser.parse_args(["good", "--flag"])
    assert handlers["good"](args) == 7
    with pytest.raises(ImportError):
        parser.parse_args(["broken"])
    with pytest.raises(ImportError):
        found[0].doctor_checks()
    assert getattr(found[0], "doctor_fix", None) is None


def test_a_module_that_must_be_imported_and_fails_keeps_its_name(package):
    pkg = package({
        "computed.py": "NAME = 'comp' + 'uted'\nraise ImportError('broken widget')\n" + CONTRACT,
        "good.py": CONTRACT,
    })
    found = commands.discover(pkg)
    assert [commands.command_name(entry) for entry in found] == ["computed", "good"]
    with pytest.raises(ImportError, match="broken widget"):
        found[0].run(None)


@pytest.mark.parametrize("source", [
    "NAME = 'x' + 'y'\n" + CONTRACT,
    CONTRACT + "\nNAME = 'late'\n",
    "from os.path import *\n" + CONTRACT,
    "from somewhere import run\n\ndef add_arguments(parser):\n    pass\n",
    "COMMANDS = []\n" + CONTRACT,
])
def test_metadata_that_is_not_plainly_static_is_imported(package, source):
    pkg = package({"odd.py": "NAME = 'odd'\n" + CONTRACT})
    path = os.path.join(pkg.__path__[0], "odd.py")
    with open(path, "w") as stream:
        stream.write(source)
    assert commands.static_metadata(f"{pkg.__name__}.odd") is None


def test_plain_metadata_is_read_without_importing(package):
    pkg = package({"plain.py": (
        '"""Doc mentioning NAME = nothing."""\n\nimport os\n\nNAME = "plain-verb"\n'
        'HELP = ("Two "\n        "lines")\n\n' + CONTRACT
    )})
    assert commands.static_metadata(f"{pkg.__name__}.plain") == {
        "NAME": "plain-verb", "HELP": "Two lines",
    }
    assert f"{pkg.__name__}.plain" not in sys.modules
