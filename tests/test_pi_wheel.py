import importlib.util
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from tests.wheel_build import build_wheel


@pytest.mark.skipif(
    importlib.util.find_spec("hatchling") is None,
    reason="hatchling is unavailable for the wheel packaging check",
)
def test_wheel_contains_loadable_pi_extension(tmp_path):
    with zipfile.ZipFile(build_wheel(tmp_path)) as archive:
        names = set(archive.namelist())
        assert "tongs_agents/pi_extension/package.json" in names
        assert (
            "tongs_agents/pi_extension/extensions/fleet-monitor.js"
            in names
        )
        extracted = tmp_path / "extracted"
        archive.extractall(extracted)

    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "from tongs_agents.pi_install import pi_extension_path; "
            "print(pi_extension_path())",
        ],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(extracted)},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert probe.returncode == 0, probe.stderr
    assert Path(probe.stdout.strip()).is_relative_to(extracted)
