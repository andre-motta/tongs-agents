"""The pinned Zellij download table used by hosted CI and the managed download."""

import importlib.resources
import importlib.util
import json
from pathlib import Path
import re
import zipfile

import pytest

from tests.wheel_build import build_wheel
from tongs_agents.terminal_apis import zellij

ROOT = Path(__file__).resolve().parents[1]
PINS_RELATIVE = "src/tongs_agents/data/zellij_pins.json"
WORKFLOW = ROOT / ".github" / "workflows" / "zellij.yml"
VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
PLATFORMS = {"linux-x86_64": "x86_64-unknown-linux-musl"}
FLAVORS = {"no-web": "zellij-no-web-", "full": "zellij-"}
REQUIRED = {"flavor", "asset", "url", "sha256", "size"}
OPTIONAL = {"binary_sha256"}
RELEASES = "https://github.com/zellij-org/zellij/releases/download/"


def _load():
    resource = importlib.resources.files("tongs_agents") / "data" / "zellij_pins.json"
    return json.loads(resource.read_text(encoding="utf-8"))


def _version(text):
    match = VERSION_RE.match(text)
    assert match, text
    return tuple(int(part) for part in match.groups())


def test_package_resource_is_the_source_file():
    assert _load() == json.loads((ROOT / PINS_RELATIVE).read_text(encoding="utf-8"))


def test_schema_is_valid():
    table = _load()

    assert set(table) == {"floor", "validated_ceiling", "pins"}
    _version(table["floor"])
    _version(table["validated_ceiling"])
    assert isinstance(table["pins"], dict) and table["pins"]
    for version, platforms in table["pins"].items():
        _version(version)
        assert isinstance(platforms, dict) and platforms, version
        for platform, entry in platforms.items():
            assert platform in PLATFORMS, platform
            assert REQUIRED <= set(entry) <= REQUIRED | OPTIONAL, (version, platform)
            prefix = FLAVORS[entry["flavor"]]
            if entry["flavor"] == "full":
                assert not entry["asset"].startswith(FLAVORS["no-web"])
            assert entry["asset"] == f"{prefix}{PLATFORMS[platform]}.tar.gz"
            assert entry["url"] == f"{RELEASES}v{version}/{entry['asset']}"
            assert SHA256_RE.match(entry["sha256"]), (version, platform)
            assert type(entry["size"]) is int and entry["size"] > 0
            if "binary_sha256" in entry:
                assert SHA256_RE.match(entry["binary_sha256"]), (version, platform)
                assert entry["binary_sha256"] != entry["sha256"]


def test_floor_and_ceiling_are_pinned_for_linux_no_web():
    table = _load()

    for version in (table["floor"], table["validated_ceiling"]):
        assert table["pins"][version]["linux-x86_64"]["flavor"] == "no-web"


def test_versions_lie_within_floor_and_ceiling():
    table = _load()
    floor = _version(table["floor"])
    ceiling = _version(table["validated_ceiling"])

    assert floor <= ceiling
    for version in table["pins"]:
        assert floor <= _version(version) <= ceiling, version


def test_adapter_validated_version_is_the_ceiling():
    assert zellij.VALIDATED_VERSION == _load()["validated_ceiling"]


def test_workflow_reads_the_table_and_verifies_before_extracting():
    text = WORKFLOW.read_text(encoding="utf-8")

    assert PINS_RELATIVE in text
    download = text.index("curl")
    verify = text.index('test "$actual_sha256" = "$expected_sha256"')
    extract = text.index("tar -xzf")
    assert download < verify < extract


@pytest.mark.skipif(
    importlib.util.find_spec("hatchling") is None,
    reason="hatchling is unavailable for the wheel packaging check",
)
def test_wheel_contains_the_pin_table(tmp_path):
    with zipfile.ZipFile(build_wheel(tmp_path)) as archive:
        packaged = json.loads(archive.read("tongs_agents/data/zellij_pins.json"))

    assert packaged == json.loads((ROOT / PINS_RELATIVE).read_text(encoding="utf-8"))
