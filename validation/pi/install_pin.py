"""Install one pinned Pi release from its verified npm tarball.

Used by the hosted Pi workflow for each pin in
src/tongs_agents/data/pi_pins.json. The tarball is downloaded over HTTPS,
its size and sha256 are checked against the pin table before npm sees it,
then npm installs it into ``--prefix``. Prints ``key=value`` metadata lines.
Standard library only; run it with ``python -I``.
"""

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PINS = REPO_ROOT / "src" / "tongs_agents" / "data" / "pi_pins.json"
REGISTRY = "https://registry.npmjs.org/"


def load_pin(version, pins_path=PINS):
    table = json.loads(Path(pins_path).read_text(encoding="utf-8"))
    entry = table["pins"].get(version)
    if entry is None:
        raise SystemExit(f"Pi {version} is not in {pins_path.name}")
    if not entry["url"].startswith(REGISTRY):
        raise SystemExit(f"refusing a Pi tarball outside {REGISTRY}")
    return table["package"], entry


def verify(path, entry):
    size = path.stat().st_size
    if size != entry["size"]:
        raise SystemExit(f"{path.name}: size {size}, expected {entry['size']}")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != entry["sha256"]:
        raise SystemExit(f"{path.name}: sha256 {digest}, expected {entry['sha256']}")
    return digest


def download(url, destination):
    request = urllib.request.Request(url, headers={"User-Agent": "tongs-agents-ci"})
    with urllib.request.urlopen(request, timeout=120) as response, \
            open(destination, "wb") as handle:
        shutil.copyfileobj(response, handle)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("version")
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--download-dir", required=True)
    arguments = parser.parse_args(argv)
    package, entry = load_pin(arguments.version)
    directory = Path(arguments.download_dir)
    directory.mkdir(parents=True, exist_ok=True)
    archive = directory / entry["url"].rsplit("/", 1)[1]
    download(entry["url"], archive)
    digest = verify(archive, entry)
    npm = shutil.which("npm")
    if npm is None:
        raise SystemExit("npm is not on PATH")
    subprocess.run([npm, "install", "--global", "--prefix", arguments.prefix,
                    "--no-audit", "--no-fund", str(archive)], check=True,
                   stdout=sys.stderr)
    pi = Path(arguments.prefix) / "bin" / "pi"
    reported = subprocess.run([str(pi), "--version"], check=True, capture_output=True,
                              text=True).stdout.strip()
    if reported != arguments.version:
        raise SystemExit(f"installed Pi reports {reported!r}, expected {arguments.version}")
    subprocess.run([npm, "ls", "--global", "--prefix", arguments.prefix, "--depth=0",
                    f"{package}@{arguments.version}"], check=True, stdout=sys.stderr)
    print(f"pi_package={package}@{arguments.version}")
    print(f"pi_tarball_sha256={digest}")
    print(f"pi_version={reported}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
