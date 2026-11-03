"""Download and install a pinned official Zellij build (Linux x86_64, no-web).

Only the explicit verbs ``tongs-agents zellij install`` and
``tongs-agents doctor --fix zellij`` call into this module, and it is the only
tongs-agents code that opens a network connection. Nothing on the hook path
imports it.

The steps, each refusing rather than guessing:

1. read the packaged pin table (``data/zellij_pins.json``);
2. download the pinned asset over HTTPS (redirects must stay HTTPS) with a
   timeout and the pinned size as a hard cap, into a new empty 0700 temporary
   directory under ``state_dir()/bin``;
3. verify the archive's sha256 before opening it; a mismatch removes the
   temporary directory, so nothing is extracted;
4. accept the archive only when every member is safe (no absolute path, no
   ``..``, no link or device) and exactly one member is the regular file
   ``zellij``; extract only that member, with tarfile's ``data`` filter when
   this Python has it, else by copying the checked member into a new file;
5. check the binary's pinned sha256, move it into a new empty 0700
   ``state_dir()/bin/zellij-<version>/`` and atomically repoint the
   ``state_dir()/bin/zellij`` symlink at it.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import posixpath
import shutil
import stat
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from tongs_agents import __version__, paths
from tongs_agents.multiplexers.zellij_binary import (
    MANAGED_NAME,
    managed_bin_dir,
    managed_link,
)

PINS_RESOURCE = ("data", "zellij_pins.json")
SUPPORTED_PLATFORM = "linux-x86_64"
FLAVOR = "no-web"
MEMBER = "zellij"
TIMEOUT_SECONDS = 60
CHUNK_BYTES = 1024 * 1024
MAX_BINARY_BYTES = 256 * 1024 * 1024
VERSION_DIR_PREFIX = "zellij-"
_X86_64 = {"x86_64", "amd64"}


class DownloadError(RuntimeError):
    """The pinned build could not be installed; nothing was changed."""


class UnsupportedPlatform(DownloadError):
    """This platform has no pinned build."""


@dataclass(frozen=True)
class Pin:
    version: str
    platform: str
    flavor: str
    asset: str
    url: str
    sha256: str
    size: int
    binary_sha256: str | None = None


@dataclass(frozen=True)
class Installed:
    version: str
    binary: Path
    link: Path
    sha256: str
    binary_sha256: str


def load_pins() -> dict:
    resource = resources.files("tongs_agents").joinpath(*PINS_RESOURCE)
    return json.loads(resource.read_text(encoding="utf-8"))


def _system() -> tuple[str, str]:
    return sys.platform, platform.machine()


def platform_key() -> str | None:
    """The pin table key for this machine, or None when no build is pinned."""
    system, machine = _system()
    if system.startswith("linux") and machine.lower() in _X86_64:
        return SUPPORTED_PLATFORM
    return None


def select_pin(version: str | None = None, table: dict | None = None) -> Pin:
    """The pinned no-web build for ``version`` (default: the validated ceiling)."""
    table = load_pins() if table is None else table
    key = platform_key()
    if key is None:
        system, machine = _system()
        raise UnsupportedPlatform(
            f"unsupported platform {system} {machine}; install zellij >= "
            f"{table['floor']} manually"
        )
    version = table["validated_ceiling"] if version is None else version
    entry = table["pins"].get(version, {}).get(key)
    if entry is None:
        raise DownloadError(
            f"zellij {version} is not pinned for {key}; pinned: "
            f"{', '.join(sorted(table['pins']))}"
        )
    if entry.get("flavor") != FLAVOR:
        raise DownloadError(
            f"zellij {version} for {key} is pinned as {entry.get('flavor')!r}, not {FLAVOR!r}"
        )
    pin = Pin(
        version=version, platform=key, flavor=entry["flavor"], asset=entry["asset"],
        url=entry["url"], sha256=entry["sha256"], size=entry["size"],
        binary_sha256=entry.get("binary_sha256"),
    )
    _require_https(pin.url)
    return pin


def _require_https(url: str) -> None:
    if urllib.parse.urlsplit(url).scheme != "https":
        raise DownloadError(f"refusing a non-HTTPS URL: {url:.200}")


class _HttpsOnlyRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _require_https(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(url: str, timeout: float):
    opener = urllib.request.build_opener(_HttpsOnlyRedirects)
    request = urllib.request.Request(url, headers={"User-Agent": f"tongs-agents/{__version__}"})
    return opener.open(request, timeout=timeout)


def download(pin: Pin, destination: Path, timeout: float = TIMEOUT_SECONDS,
             opener=None) -> str:
    """Fetch ``pin.url`` into ``destination`` (a new file); return its sha256.

    Reads at most ``pin.size`` bytes plus one, so an oversized response is
    refused without being stored whole.
    """
    _require_https(pin.url)
    opener = _open if opener is None else opener
    digest = hashlib.sha256()
    received = 0
    try:
        with opener(pin.url, timeout) as response:
            final = response.geturl() if hasattr(response, "geturl") else pin.url
            _require_https(final or pin.url)
            declared = response.headers.get("Content-Length") if response.headers else None
            if declared is not None and declared.isdigit() and int(declared) != pin.size:
                raise DownloadError(
                    f"{pin.asset}: server announced {declared} bytes, pinned size is {pin.size}"
                )
            fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                while True:
                    chunk = response.read(min(CHUNK_BYTES, pin.size + 1 - received))
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > pin.size:
                        raise DownloadError(
                            f"{pin.asset}: larger than the pinned {pin.size} bytes"
                        )
                    digest.update(chunk)
                    stream.write(chunk)
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise DownloadError(f"cannot download {pin.url}: {error}") from None
    if received != pin.size:
        raise DownloadError(f"{pin.asset}: got {received} bytes, pinned size is {pin.size}")
    return digest.hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def unsafe_member(member: tarfile.TarInfo) -> str | None:
    """Why ``member`` is refused, or None for a plain relative file or directory."""
    name = member.name
    if not name or "\x00" in name:
        return "empty or NUL name"
    if name.startswith(("/", "\\")) or posixpath.isabs(name) or (len(name) > 1 and name[1] == ":"):
        return "absolute path"
    if ".." in name.replace("\\", "/").split("/"):
        return "'..' component"
    if member.issym() or member.islnk():
        return "link"
    if member.ischr() or member.isblk() or member.isfifo() or member.isdev():
        return "device or fifo"
    if not (member.isfile() or member.isdir()):
        return "unsupported member type"
    return None


def _binary_member(archive: tarfile.TarFile) -> tarfile.TarInfo:
    found = []
    for member in archive.getmembers():
        reason = unsafe_member(member)
        if reason is not None:
            raise DownloadError(f"refusing archive member {member.name!r:.120}: {reason}")
        if posixpath.normpath(member.name) == MEMBER:
            found.append(member)
    if len(found) != 1 or not found[0].isfile():
        raise DownloadError(f"the archive must hold exactly one regular file {MEMBER!r}")
    if found[0].size > MAX_BINARY_BYTES:
        raise DownloadError(f"{MEMBER!r} is larger than {MAX_BINARY_BYTES} bytes")
    return found[0]


def _data_filter_available() -> bool:
    return hasattr(tarfile, "data_filter")


def _copy_member(archive: tarfile.TarFile, member: tarfile.TarInfo, target: Path) -> None:
    """Without the data filter, write the already-checked member ourselves."""
    source = archive.extractfile(member)
    if source is None:
        raise DownloadError(f"cannot read {member.name!r} from the archive")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    with source, os.fdopen(os.open(target, flags, 0o700), "wb") as stream:
        shutil.copyfileobj(source, stream, CHUNK_BYTES)


def extract_binary(archive_path: Path, destination: Path) -> Path:
    """Extract only the ``zellij`` member into the empty ``destination``."""
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            member = _binary_member(archive)
            member.name = MEMBER
            if _data_filter_available():
                archive.extract(member, destination, filter="data")
            else:
                _copy_member(archive, member, destination / MEMBER)
    except (tarfile.TarError, EOFError, OSError) as error:
        raise DownloadError(f"cannot extract {archive_path.name}: {error}") from None
    binary = destination / MEMBER
    info = os.lstat(binary)
    if not stat.S_ISREG(info.st_mode):
        raise DownloadError(f"extracted {MEMBER!r} is not a regular file")
    os.chmod(binary, 0o700)
    return binary


def _new_private_dir(parent: Path, prefix: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix, dir=parent))


def _replace_version_dir(staging: Path, target: Path) -> None:
    """Move ``staging`` to ``target``; an earlier managed copy is set aside first."""
    old = None
    if os.path.lexists(target):
        info = os.lstat(target)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise DownloadError(f"{target} is not a directory tongs-agents owns; move it aside")
        old = _new_private_dir(target.parent, f".{target.name}.old-")
        os.rename(target, old / "previous")
    try:
        os.rename(staging, target)
    except OSError:
        if old is not None:
            os.rename(old / "previous", target)
            shutil.rmtree(old, ignore_errors=True)
        raise
    if old is not None:
        shutil.rmtree(old, ignore_errors=True)


def repoint_link(link: Path, version_dir: Path) -> None:
    """Atomically make ``link`` a relative symlink to ``version_dir/zellij``."""
    if os.path.lexists(link) and not link.is_symlink():
        raise DownloadError(f"{link} exists and is not a symlink; move it aside")
    target = os.path.join(version_dir.name, MEMBER)
    scratch = _new_private_dir(link.parent, ".link-")
    try:
        temporary = scratch / MANAGED_NAME
        os.symlink(target, temporary)
        os.replace(temporary, link)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def install(version: str | None = None, opener=None, timeout: float = TIMEOUT_SECONDS,
            report=None) -> Installed:
    """Install the pinned build and repoint the managed link; refuse on any doubt."""
    report = report or (lambda message: None)
    pin = select_pin(version)
    paths.state_dir(create=True)
    bin_dir = paths.ensure_private_dir(managed_bin_dir())
    work = _new_private_dir(bin_dir, ".download-")
    try:
        archive = work / pin.asset
        report(f"Downloading {pin.url}")
        actual = download(pin, archive, timeout=timeout, opener=opener)
        if actual != pin.sha256:
            raise DownloadError(
                f"{pin.asset}: sha256 {actual} does not match the pinned {pin.sha256}; "
                "nothing was extracted"
            )
        report(f"Verified sha256 {actual}")
        staging = work / "staging"
        staging.mkdir(mode=0o700)
        binary = extract_binary(archive, staging)
        binary_sha = file_sha256(binary)
        if pin.binary_sha256 is not None and binary_sha != pin.binary_sha256:
            raise DownloadError(
                f"extracted zellij sha256 {binary_sha} does not match the pinned "
                f"{pin.binary_sha256}"
            )
        version_dir = bin_dir / f"{VERSION_DIR_PREFIX}{pin.version}"
        _replace_version_dir(staging, version_dir)
        os.chmod(version_dir, 0o700)
        link = managed_link()
        repoint_link(link, version_dir)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return Installed(
        version=pin.version, binary=version_dir / MEMBER, link=link,
        sha256=actual, binary_sha256=binary_sha,
    )


__all__ = [
    "DownloadError", "Installed", "Pin", "UnsupportedPlatform", "download",
    "extract_binary", "install", "load_pins", "platform_key",
    "repoint_link", "select_pin", "unsafe_member",
]
