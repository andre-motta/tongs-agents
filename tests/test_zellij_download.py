"""Managed pinned Zellij download, with local fake assets only (no network)."""

import copy
import hashlib
import io
import json
import os
import stat
import subprocess
import sys
import tarfile
import textwrap

import pytest

from tongs_agents import cli, paths, zellij_download
from tongs_agents.commands import zellij as zellij_command
from tongs_agents.multiplexers import zellij_binary

URL = "https://example.invalid/releases/zellij-no-web-x86_64-unknown-linux-musl.tar.gz"
FAKE_BINARY = b"#!/bin/sh\necho 'zellij 0.45.1'\n"


def _tar(*members):
    """A gzip tarball of ``(TarInfo kwargs, data)`` pairs."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for fields, data in members:
            info = tarfile.TarInfo(fields.pop("name"))
            for key, value in fields.items():
                setattr(info, key, value)
            if data is not None:
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
            else:
                archive.addfile(info)
    return buffer.getvalue()


def _binary_tar(data=FAKE_BINARY, name="zellij"):
    return _tar(({"name": name, "mode": 0o755}, data))


def _table(asset, version="0.45.1", binary=FAKE_BINARY, **overrides):
    entry = {
        "flavor": "no-web",
        "asset": "zellij-no-web-x86_64-unknown-linux-musl.tar.gz",
        "url": URL,
        "sha256": hashlib.sha256(asset).hexdigest(),
        "size": len(asset),
        "binary_sha256": hashlib.sha256(binary).hexdigest(),
    }
    entry.update(overrides)
    return {"floor": "0.44.2", "validated_ceiling": version, "pins": {version: {"linux-x86_64": entry}}}


class _Response(io.BytesIO):
    def __init__(self, data, url=URL, length=None):
        super().__init__(data)
        self._url = url
        self.headers = {} if length is None else {"Content-Length": str(length)}

    def geturl(self):
        return self._url


class _Opener:
    def __init__(self, data, **kwargs):
        self.data = data
        self.kwargs = kwargs
        self.calls = []

    def __call__(self, url, timeout):
        self.calls.append((url, timeout))
        return _Response(self.data, **self.kwargs)


@pytest.fixture
def linux(monkeypatch):
    monkeypatch.setattr(zellij_download, "_system", lambda: ("linux", "x86_64"))


@pytest.fixture
def no_path_zellij(monkeypatch):
    monkeypatch.setattr(zellij_binary.shutil, "which", lambda name: None)


def _use(monkeypatch, table):
    monkeypatch.setattr(zellij_download, "load_pins", lambda: copy.deepcopy(table))


def _bin_dir():
    return paths.state_dir() / "bin"


def _bin_entries():
    directory = _bin_dir()
    return sorted(os.listdir(directory)) if directory.exists() else []


def _mode(path):
    return stat.S_IMODE(os.lstat(path).st_mode)


def test_install_places_binary_in_private_dirs_and_links_it(linux, no_path_zellij, monkeypatch):
    asset = _binary_tar()
    _use(monkeypatch, _table(asset))
    opener = _Opener(asset)

    installed = zellij_download.install(opener=opener)

    version_dir = _bin_dir() / "zellij-0.45.1"
    assert opener.calls == [(URL, zellij_download.TIMEOUT_SECONDS)]
    assert installed.binary == version_dir / "zellij"
    assert installed.binary.read_bytes() == FAKE_BINARY
    assert _mode(version_dir) == 0o700
    assert _mode(_bin_dir()) == 0o700
    assert _mode(installed.binary) == 0o700
    assert os.readlink(installed.link) == os.path.join("zellij-0.45.1", "zellij")
    assert _bin_entries() == ["zellij", "zellij-0.45.1"]
    assert zellij_binary.zellij_binary() == str(installed.link)


def test_mismatched_sha_refuses_with_nothing_extracted(linux, monkeypatch):
    asset = _binary_tar()
    _use(monkeypatch, _table(asset, sha256="0" * 64))

    with pytest.raises(zellij_download.DownloadError, match="nothing was extracted"):
        zellij_download.install(opener=_Opener(asset))

    assert _bin_entries() == []


def test_extraction_is_never_attempted_on_a_mismatch(linux, monkeypatch):
    asset = _binary_tar()
    _use(monkeypatch, _table(asset, sha256="0" * 64))
    monkeypatch.setattr(
        zellij_download, "extract_binary",
        lambda *args: pytest.fail("extracted before verification"),
    )

    with pytest.raises(zellij_download.DownloadError):
        zellij_download.install(opener=_Opener(asset))


@pytest.mark.parametrize("member", [
    {"name": "../zellij", "mode": 0o755},
    {"name": "bin/../../zellij", "mode": 0o755},
    {"name": "/tmp/zellij", "mode": 0o755},
])
def test_traversal_member_is_refused(linux, monkeypatch, tmp_path, member):
    asset = _tar(({"name": "zellij", "mode": 0o755}, FAKE_BINARY), (member, FAKE_BINARY))
    _use(monkeypatch, _table(asset))

    with pytest.raises(zellij_download.DownloadError, match="refusing archive member"):
        zellij_download.install(opener=_Opener(asset))

    assert _bin_entries() == []
    assert not (paths.state_dir() / "zellij").exists()


@pytest.mark.parametrize("fields,reason", [
    ({"name": "zellij", "type": tarfile.SYMTYPE, "linkname": "/bin/sh"}, "link"),
    ({"name": "zellij", "type": tarfile.LNKTYPE, "linkname": "other"}, "link"),
    ({"name": "zellij", "type": tarfile.CHRTYPE, "devmajor": 1, "devminor": 3}, "device"),
    ({"name": "zellij", "type": tarfile.BLKTYPE, "devmajor": 8, "devminor": 0}, "device"),
    ({"name": "zellij", "type": tarfile.FIFOTYPE}, "device or fifo"),
])
def test_links_and_devices_are_refused(linux, monkeypatch, fields, reason):
    asset = _tar((dict(fields), None))
    _use(monkeypatch, _table(asset))

    with pytest.raises(zellij_download.DownloadError, match=reason):
        zellij_download.install(opener=_Opener(asset))

    assert _bin_entries() == []


@pytest.mark.parametrize("members", [
    [({"name": "zellij", "mode": 0o755}, FAKE_BINARY), ({"name": "./zellij", "mode": 0o755}, FAKE_BINARY)],
    [({"name": "other", "mode": 0o755}, FAKE_BINARY)],
    [({"name": "zellij", "type": tarfile.DIRTYPE}, None)],
])
def test_archive_needs_exactly_one_regular_zellij(linux, monkeypatch, members):
    asset = _tar(*[(dict(fields), data) for fields, data in members])
    _use(monkeypatch, _table(asset))

    with pytest.raises(zellij_download.DownloadError, match="exactly one regular file"):
        zellij_download.install(opener=_Opener(asset))

    assert _bin_entries() == []


def test_only_the_zellij_member_is_extracted(linux, no_path_zellij, monkeypatch):
    asset = _tar(
        ({"name": "LICENSE", "mode": 0o644}, b"license"),
        ({"name": "./zellij", "mode": 0o755}, FAKE_BINARY),
    )
    _use(monkeypatch, _table(asset))

    installed = zellij_download.install(opener=_Opener(asset))

    assert sorted(os.listdir(installed.binary.parent)) == ["zellij"]


def test_manual_check_still_refuses_without_the_data_filter(linux, monkeypatch):
    monkeypatch.setattr(zellij_download, "_data_filter_available", lambda: False)
    asset = _tar(({"name": "zellij", "mode": 0o755}, FAKE_BINARY), ({"name": "../x"}, b"x"))
    _use(monkeypatch, _table(asset))

    with pytest.raises(zellij_download.DownloadError, match="'..' component"):
        zellij_download.install(opener=_Opener(asset))
    assert _bin_entries() == []


def test_manual_path_installs_without_the_data_filter(linux, no_path_zellij, monkeypatch):
    monkeypatch.setattr(zellij_download, "_data_filter_available", lambda: False)
    asset = _binary_tar()
    _use(monkeypatch, _table(asset))

    installed = zellij_download.install(opener=_Opener(asset))

    assert installed.binary.read_bytes() == FAKE_BINARY


def test_binary_sha_mismatch_is_refused(linux, monkeypatch):
    asset = _binary_tar()
    _use(monkeypatch, _table(asset, binary_sha256="1" * 64))

    with pytest.raises(zellij_download.DownloadError, match="extracted zellij sha256"):
        zellij_download.install(opener=_Opener(asset))
    assert _bin_entries() == []


@pytest.mark.parametrize("opener_kwargs,data_extra,match", [
    ({}, b"x", "larger than the pinned"),
    ({"length": 1}, b"", "server announced"),
    ({"url": "http://example.invalid/x"}, b"", "non-HTTPS"),
])
def test_download_size_cap_and_https(linux, monkeypatch, opener_kwargs, data_extra, match):
    asset = _binary_tar()
    _use(monkeypatch, _table(asset))

    with pytest.raises(zellij_download.DownloadError, match=match):
        zellij_download.install(opener=_Opener(asset + data_extra, **opener_kwargs))
    assert _bin_entries() == []


def test_short_download_is_refused(linux, monkeypatch):
    asset = _binary_tar()
    _use(monkeypatch, _table(asset))

    with pytest.raises(zellij_download.DownloadError, match="got"):
        zellij_download.install(opener=_Opener(asset[:-1]))


def test_non_https_pin_is_refused(linux, monkeypatch):
    asset = _binary_tar()
    _use(monkeypatch, _table(asset, url="http://example.invalid/zellij.tar.gz"))

    with pytest.raises(zellij_download.DownloadError, match="non-HTTPS"):
        zellij_download.install(opener=_Opener(asset))


def test_redirects_must_stay_https():
    handler = zellij_download._HttpsOnlyRedirects()

    with pytest.raises(zellij_download.DownloadError, match="non-HTTPS"):
        handler.redirect_request(None, None, 302, "Found", {}, "http://example.invalid/x")


def test_reinstall_and_upgrade_repoint_the_link(linux, no_path_zellij, monkeypatch):
    old = b"#!/bin/sh\necho 'zellij 0.44.2'\n"
    old_asset = _binary_tar(old)
    _use(monkeypatch, _table(old_asset, version="0.44.2", binary=old))
    zellij_download.install(opener=_Opener(old_asset))
    new_asset = _binary_tar()
    table = _table(new_asset)
    table["pins"].update(_table(old_asset, version="0.44.2", binary=old)["pins"])
    _use(monkeypatch, table)

    zellij_download.install(opener=_Opener(new_asset))
    installed = zellij_download.install(opener=_Opener(new_asset))

    assert os.readlink(installed.link) == os.path.join("zellij-0.45.1", "zellij")
    assert _bin_entries() == ["zellij", "zellij-0.44.2", "zellij-0.45.1"]
    assert installed.link.read_bytes() == FAKE_BINARY


def test_link_that_is_not_a_symlink_is_refused(linux, monkeypatch):
    asset = _binary_tar()
    _use(monkeypatch, _table(asset))
    bin_dir = paths.ensure_private_dir(paths.state_dir(create=True) / "bin")
    (bin_dir / "zellij").write_text("mine")

    with pytest.raises(zellij_download.DownloadError, match="not a symlink"):
        zellij_download.install(opener=_Opener(asset))
    assert (bin_dir / "zellij").read_text() == "mine"
    assert not any(name.startswith(".") for name in os.listdir(bin_dir))


@pytest.mark.parametrize("system", [("darwin", "arm64"), ("linux", "aarch64"), ("win32", "AMD64")])
def test_other_platforms_are_unsupported(monkeypatch, system):
    monkeypatch.setattr(zellij_download, "_system", lambda: system)

    assert zellij_download.platform_key() is None
    with pytest.raises(zellij_download.UnsupportedPlatform, match="install zellij >= 0.44.2 manually"):
        zellij_download.select_pin()


def test_default_pin_is_the_packaged_validated_ceiling(linux):
    table = zellij_download.load_pins()
    pin = zellij_download.select_pin()

    assert pin.version == table["validated_ceiling"]
    assert pin.flavor == "no-web"
    assert pin.url.startswith("https://github.com/zellij-org/zellij/releases/download/")


def _cli(monkeypatch, capsys, *argv):
    capsys.readouterr()
    monkeypatch.setattr(sys, "argv", ["tongs-agents", *argv])
    try:
        cli.main()
        code = 0
    except SystemExit as raised:
        code = raised.code
    out = capsys.readouterr()
    return code, out.out, out.err


@pytest.mark.parametrize("argv", [("zellij", "install"), ("doctor", "--fix", "zellij")])
def test_verbs_refuse_unsupported_platforms_with_exit_2(monkeypatch, capsys, no_path_zellij, argv):
    monkeypatch.setattr(zellij_download, "_system", lambda: ("darwin", "arm64"))
    monkeypatch.setattr(
        zellij_download, "install", lambda *args, **kwargs: pytest.fail("installed"),
    )

    code, _, err = _cli(monkeypatch, capsys, *argv)

    assert code == 2
    assert "unsupported, install zellij >= 0.44.2 manually" in err


@pytest.mark.parametrize("argv", [("zellij", "install"), ("doctor", "--fix", "zellij")])
def test_verbs_install_the_pinned_build(linux, no_path_zellij, monkeypatch, capsys, argv):
    asset = _binary_tar()
    _use(monkeypatch, _table(asset))
    real_install = zellij_download.install
    monkeypatch.setattr(
        zellij_download, "install",
        lambda version=None, **kwargs: real_install(version, opener=_Opener(asset), **kwargs),
    )

    code, out, _ = _cli(monkeypatch, capsys, *argv)

    assert code == 0, out
    assert "Installed zellij 0.45.1" in out
    assert zellij_binary.zellij_binary() == str(_bin_dir() / "zellij")


def test_install_failure_exits_1_and_changes_nothing(linux, monkeypatch, capsys):
    asset = _binary_tar()
    _use(monkeypatch, _table(asset, sha256="0" * 64))
    real_install = zellij_download.install
    monkeypatch.setattr(
        zellij_download, "install",
        lambda version=None, **kwargs: real_install(version, opener=_Opener(asset), **kwargs),
    )

    code, _, err = _cli(monkeypatch, capsys, "zellij", "install")

    assert code == 1
    assert "does not match the pinned" in err
    assert _bin_entries() == []


def _fake_zellij(directory, version):
    directory.mkdir(parents=True, exist_ok=True)
    binary = directory / "zellij"
    binary.write_text(f"#!/bin/sh\necho 'zellij {version}'\n")
    binary.chmod(0o755)
    return str(binary)


def _managed(version):
    bin_dir = paths.ensure_private_dir(paths.state_dir(create=True) / "bin")
    _fake_zellij(bin_dir / f"zellij-{version}", version)
    os.symlink(f"zellij-{version}/zellij", bin_dir / "zellij")
    return str(bin_dir / "zellij")


@pytest.mark.skipif(sys.platform == "win32", reason="shell fakes")
@pytest.mark.parametrize("path_version,managed,expected", [
    ("0.45.1", True, "path"),
    ("0.44.2", True, "path"),
    ("0.40.0", True, "managed"),
    (None, True, "managed"),
    ("0.40.0", False, "path"),
    (None, False, None),
])
def test_binary_order_prefers_path_at_or_above_the_floor(
        tmp_path, monkeypatch, path_version, managed, expected):
    found = _fake_zellij(tmp_path / "path", path_version) if path_version else None
    monkeypatch.setattr(zellij_binary.shutil, "which", lambda name: found)
    link = _managed("0.45.1") if managed else None

    assert zellij_binary.zellij_binary() == {"path": found, "managed": link, None: None}[expected]


def test_broken_managed_link_is_ignored_and_reported(no_path_zellij, linux):
    bin_dir = paths.ensure_private_dir(paths.state_dir(create=True) / "bin")
    os.symlink("zellij-0.45.1/zellij", bin_dir / "zellij")

    assert zellij_binary.zellij_binary() is None
    checks = zellij_command.doctor_checks()
    assert [check.level for check in checks] == ["warn", "info"]
    assert checks[0].remediation == zellij_command.FIX_COMMAND


@pytest.mark.skipif(sys.platform == "win32", reason="shell fakes")
def test_doctor_check_reports_managed_binary(no_path_zellij):
    _managed("0.45.1")

    checks = zellij_command.doctor_checks()

    assert [(check.level, check.message) for check in checks] == [
        ("ok", "zellij 0.45.1 from managed"),
    ]


def test_doctor_check_offers_the_fix_or_manual_install(no_path_zellij, monkeypatch):
    monkeypatch.setattr(zellij_download, "_system", lambda: ("linux", "x86_64"))
    assert zellij_command.doctor_checks()[0].remediation == zellij_command.FIX_COMMAND
    monkeypatch.setattr(zellij_download, "_system", lambda: ("darwin", "arm64"))
    assert "install zellij >= 0.44.2 manually" in zellij_command.doctor_checks()[0].message


@pytest.mark.skipif(sys.platform == "win32", reason="shell fakes")
def test_doctor_fix_does_nothing_when_zellij_meets_the_floor(tmp_path, monkeypatch, capsys):
    found = _fake_zellij(tmp_path / "path", "0.45.1")
    monkeypatch.setattr(zellij_binary.shutil, "which", lambda name: found)
    monkeypatch.setattr(
        zellij_download, "install", lambda *args, **kwargs: pytest.fail("installed"),
    )

    code, out, _ = _cli(monkeypatch, capsys, "doctor", "--fix", "zellij")

    assert code == 0
    assert "nothing to fix" in out


HOOK_PROBE = textwrap.dedent("""
    import io, json, socket, sys
    attempts = []
    def refuse(*args, **kwargs):
        attempts.append(args)
        raise OSError("network blocked by test")
    socket.socket = refuse
    socket.create_connection = refuse
    sys.stdin = io.StringIO(json.dumps({"session_id": "probe-1", "cwd": "/tmp/probe"}))
    sys.argv = ["tongs-agents-hook", "session-start", "--agent", "claude"]
    import tongs_agents.hook as hook
    try:
        hook.main()
    except SystemExit:
        pass
    print(json.dumps({
        "attempts": len(attempts),
        "download_imported": "tongs_agents.zellij_download" in sys.modules,
        "urllib_request_imported": "urllib.request" in sys.modules,
    }))
""")


def test_hook_never_imports_the_downloader_or_opens_a_socket(tmp_path):
    env = {
        key: value for key, value in os.environ.items()
        if key not in ("TMUX", "ZELLIJ", "KITTY_LISTEN_ON")
    }
    env[paths.STATE_DIR_ENV] = str(tmp_path / "state")
    env["HOME"] = str(tmp_path / "home")
    result = subprocess.run(
        [sys.executable, "-c", HOOK_PROBE],
        capture_output=True, text=True, timeout=30, check=True, env=env,
    )
    report = json.loads(result.stdout.strip().splitlines()[-1])

    assert report == {"attempts": 0, "download_imported": False, "urllib_request_imported": False}
    assert list((tmp_path / "state" / "sessions").glob("*.json"))
