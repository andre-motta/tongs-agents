"""The built wheel ships every runtime file, and the installed code finds them.

CI installs the project editable, so a packaging rule that drops a data file
(the workspace layout template, a pin table, the Pi extension) would pass
every other test and break the PyPI install. These tests build the real wheel
and read its contents, its entry points and its metadata, then import the
extracted wheel in a subprocess and load each file the way the code does.
"""

import configparser
import importlib.util
from email.parser import HeaderParser
import json
import os
from pathlib import Path
import subprocess
import sys
import tomllib
import zipfile

import pytest

from tests.wheel_build import build_wheel

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "tongs_agents"
RUNTIME_DATA = (
    "tongs_agents/layouts/agents.kdl.tmpl",
    "tongs_agents/data/pi_pins.json",
    "tongs_agents/data/zellij_pins.json",
    "tongs_agents/pi_extension/package.json",
    "tongs_agents/pi_extension/extensions/fleet-monitor.js",
)
# Run from the extracted wheel: each value printed must come from there.
PROBE = """
import json
from importlib import resources

import tongs_agents
from tongs_agents import zellij_download
from tongs_agents.commands import workspace
from tongs_agents.pi_install import pi_extension_path

package = resources.files("tongs_agents")
print(json.dumps({
    "module": tongs_agents.__file__,
    "version": tongs_agents.__version__,
    "template": workspace.load_template(),
    "zellij_pins": zellij_download.load_pins(),
    "pi_pins": json.loads(package.joinpath("data", "pi_pins.json").read_text("utf-8")),
    "pi_extension": str(pi_extension_path()),
}))
"""

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("hatchling") is None,
    reason="hatchling is unavailable for the wheel packaging check",
)


@pytest.fixture(scope="module")
def wheel(tmp_path_factory):
    return build_wheel(tmp_path_factory.mktemp("wheel-contents"))


@pytest.fixture(scope="module")
def names(wheel):
    with zipfile.ZipFile(wheel) as archive:
        return set(archive.namelist())


def _pyproject():
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]


def _dist_info(wheel, name):
    with zipfile.ZipFile(wheel) as archive:
        matches = [n for n in archive.namelist()
                   if n.endswith(f".dist-info/{name}") and n.count("/") == 1]
        assert len(matches) == 1, matches
        return archive.read(matches[0]).decode("utf-8")


def _source_data_files():
    return sorted(
        "tongs_agents/" + path.relative_to(PACKAGE).as_posix()
        for path in PACKAGE.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
        and path.suffix not in {".py", ".pyc"}
    )


def test_wheel_ships_the_runtime_data_files(names):
    assert set(RUNTIME_DATA) <= names


def test_wheel_ships_every_non_python_file_of_the_package(wheel, names):
    data = _source_data_files()
    assert set(RUNTIME_DATA) <= set(data)
    with zipfile.ZipFile(wheel) as archive:
        for name in data:
            assert name in names, name
            assert archive.read(name) == (ROOT / "src" / name).read_bytes(), name


def test_wheel_ships_every_python_module_and_nothing_outside_the_package(names):
    modules = {
        "tongs_agents/" + path.relative_to(PACKAGE).as_posix()
        for path in PACKAGE.rglob("*.py") if "__pycache__" not in path.parts
    }
    assert modules - {"tongs_agents/_version.py"} <= names
    assert "tongs_agents/_version.py" in names
    outside = {name for name in names
               if not name.startswith("tongs_agents/") and ".dist-info/" not in name}
    assert outside == set()


def test_wheel_entry_points_match_pyproject(wheel):
    project = _pyproject()
    parser = configparser.ConfigParser(delimiters=("=",))
    parser.optionxform = str
    parser.read_string(_dist_info(wheel, "entry_points.txt"))

    scripts = {key: value.strip() for key, value in parser["console_scripts"].items()}
    plugins = {key: value.strip() for key, value in parser["tongs.plugins"].items()}
    assert scripts == project["scripts"] == {
        "tongs-agents": "tongs_agents.cli:main",
        "tongs-agents-hook": "tongs_agents.hook:main",
    }
    assert plugins == project["entry-points"]["tongs.plugins"] == {
        "agents": "tongs_agents.tongs_plugin:FleetMonitorPlugin",
    }


def test_wheel_metadata_states_the_python_floor_and_dependencies(wheel):
    project = _pyproject()
    metadata = HeaderParser().parsestr(_dist_info(wheel, "METADATA"))

    assert metadata["Name"] == "tongs-agents"
    assert metadata["Requires-Python"] == project["requires-python"] == ">=3.11"
    requires = metadata.get_all("Requires-Dist")
    for dependency in project["dependencies"]:
        name = dependency.split(">", 1)[0].split("[", 1)[0]
        assert any(line.startswith(name) for line in requires), dependency
    assert metadata["Description-Content-Type"] == "text/markdown"


def test_extracted_wheel_loads_its_files_through_importlib_resources(wheel, tmp_path):
    extracted = tmp_path / "extracted"
    with zipfile.ZipFile(wheel) as archive:
        archive.extractall(extracted)

    probe = subprocess.run(
        [sys.executable, "-c", PROBE],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(extracted)},
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert probe.returncode == 0, probe.stderr
    loaded = json.loads(probe.stdout)
    assert Path(loaded["module"]).is_relative_to(extracted)
    assert loaded["version"] == "0.0.0"
    assert loaded["template"] == (PACKAGE / "layouts" / "agents.kdl.tmpl").read_text(
        encoding="utf-8")
    assert loaded["zellij_pins"] == json.loads(
        (PACKAGE / "data" / "zellij_pins.json").read_text(encoding="utf-8"))
    assert loaded["pi_pins"] == json.loads(
        (PACKAGE / "data" / "pi_pins.json").read_text(encoding="utf-8"))
    assert Path(loaded["pi_extension"]).is_relative_to(extracted)
