"""Build the project wheel from a private copy of the source tree.

The hatch-vcs build hook writes src/tongs_agents/_version.py into the tree it
builds. Building from a copy keeps the checkout's version file (and so an
editable install's ``__version__``) untouched by the packaging tests.
"""

import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SOURCE_FILES = ("pyproject.toml", "README.md", "LICENSE", ".gitignore")
IGNORED = shutil.ignore_patterns("__pycache__", "*.py[cod]", "_version.py")


def copy_source(destination):
    destination.mkdir()
    for name in SOURCE_FILES:
        shutil.copy2(ROOT / name, destination / name)
    shutil.copytree(ROOT / "src", destination / "src", ignore=IGNORED)
    return destination


def build_wheel(tmp_path):
    """Build one wheel from a copy under tmp_path and return its path."""
    source = copy_source(tmp_path / "source")
    wheel_dir = tmp_path / "wheel"
    wheel_dir.mkdir()
    result = subprocess.run(
        [
            sys.executable, "-m", "pip", "wheel", str(source),
            "--no-deps", "--no-build-isolation", "--wheel-dir", str(wheel_dir),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        env={
            **os.environ,
            "SETUPTOOLS_SCM_PRETEND_VERSION_FOR_TONGS_AGENTS": "0.0.0",
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr
    wheels = list(wheel_dir.glob("*.whl"))
    assert len(wheels) == 1
    return wheels[0]
