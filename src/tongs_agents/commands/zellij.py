"""``tongs-agents zellij``: show which zellij the workspace uses, or install one.

``tongs-agents zellij install`` (and ``tongs-agents doctor --fix zellij``,
which runs only when zellij is missing or below the floor) installs the pinned
official no-web build for Linux x86_64 through ``zellij_download``.
``tongs-agents zellij install --web`` installs the pinned full build with the
web server instead, beside it; only ``tongs-agents web`` uses it, and only
while web watching is enabled. These verbs are the only tongs-agents code that
touches the network. Other platforms are refused with exit status 2.
"""

from __future__ import annotations

import os
import sys

from tongs_agents import zellij_download
from tongs_agents.commands.doctor import Check
from tongs_agents.multiplexers.zellij_binary import (
    ZELLIJ_FLOOR,
    format_version,
    managed_binary,
    managed_link,
    managed_web_binary,
    path_binary,
    web_enabled,
    zellij_binary,
    zellij_version,
)
from tongs_agents.paths import UnsafePathError

NAME = "zellij"
HELP = "Show the zellij the workspace uses, or install a pinned build"
FIX_COMMAND = "tongs-agents doctor --fix zellij"
WEB_INSTALL_COMMAND = "tongs-agents zellij install --web"
EXIT_ERROR = 1
EXIT_UNSUPPORTED = 2


def add_arguments(parser):
    actions = parser.add_subparsers(dest="zellij_action")
    actions.add_parser("status", help="Show the zellij in use (default)")
    install = actions.add_parser(
        "install",
        help="Download, verify and install the pinned zellij build (Linux x86_64 only)",
    )
    install.add_argument(
        "--version", dest="zellij_version", default=None, metavar="X.Y.Z",
        help="Pinned version to install (default: the validated ceiling)",
    )
    install.add_argument(
        "--web", action="store_true",
        help="Install the pinned full build with the web server, used only by "
             "'tongs-agents web' while web watching is enabled",
    )


def _floor() -> str:
    return format_version(ZELLIJ_FLOOR)


def _unsupported_message() -> str:
    return f"unsupported, install zellij >= {_floor()} manually"


def _resolved() -> tuple[str | None, tuple[int, int, int] | None]:
    binary = zellij_binary()
    return binary, (zellij_version(binary) if binary else None)


def _source(binary: str | None) -> str:
    if binary is not None and binary == managed_web_binary():
        return "managed web build, web watching enabled"
    if binary is not None and binary == managed_binary():
        return "managed"
    return "PATH"


def needs_install() -> bool:
    """True when no zellij at or above the floor is available."""
    binary, version = _resolved()
    return binary is None or version is None or version < ZELLIJ_FLOOR


def install(version: str | None = None, web: bool = False) -> int:
    if zellij_download.platform_key() is None:
        print(f"tongs-agents zellij install: {_unsupported_message()}", file=sys.stderr)
        return EXIT_UNSUPPORTED
    flavor = zellij_download.WEB if web else zellij_download.NO_WEB
    try:
        installed = zellij_download.install(version, report=print, flavor=flavor)
    except zellij_download.UnsupportedPlatform:
        print(f"tongs-agents zellij install: {_unsupported_message()}", file=sys.stderr)
        return EXIT_UNSUPPORTED
    except (zellij_download.DownloadError, UnsafePathError, OSError) as error:
        print(f"tongs-agents zellij install: {error}", file=sys.stderr)
        return EXIT_ERROR
    print(f"Installed zellij {installed.version} ({flavor}) at {installed.binary}")
    print(f"Linked {installed.link} -> {os.readlink(installed.link)}")
    if web:
        print("The web build is used only while web watching is enabled "
              "('tongs-agents web enable')")
        return 0
    found = path_binary()
    found_version = zellij_version(found) if found else None
    if found_version is not None and found_version >= ZELLIJ_FLOOR:
        print(
            f"Note: {found} (zellij {format_version(found_version)}) on PATH meets the "
            "floor and stays preferred"
        )
    return 0


def doctor_fix(args=None) -> int:
    if not needs_install():
        binary, version = _resolved()
        print(f"zellij {format_version(version)} at {binary} meets the floor; nothing to fix")
        return 0
    return install()


def _status() -> int:
    binary, version = _resolved()
    if binary is None:
        print(f"zellij: not found (needs {_floor()} or newer); run '{FIX_COMMAND}'")
        return EXIT_ERROR
    shown = format_version(version) if version else "unknown version"
    print(f"zellij {shown} at {binary} ({_source(binary)})")
    web = managed_web_binary()
    if web is not None and web != binary:
        state = "enabled" if web_enabled() else "not enabled"
        print(f"Web build at {web} (used while web watching is enabled; {state})")
    if version is None or version < ZELLIJ_FLOOR:
        print(f"Below the supported {_floor()}; run '{FIX_COMMAND}'")
        return EXIT_ERROR
    return 0


def run(args) -> int:
    if getattr(args, "zellij_action", None) == "install":
        return install(args.zellij_version, web=getattr(args, "web", False))
    return _status()


def doctor_checks():
    """Offer the managed download when zellij is missing or below the floor."""
    checks = []
    link = managed_link()
    if os.path.lexists(link) and managed_binary() is None:
        checks.append(Check(
            "zellij-managed", "warn",
            f"The managed zellij link {link} does not resolve to an executable",
            FIX_COMMAND if zellij_download.platform_key() else _unsupported_message(),
        ))
    if not needs_install():
        binary, version = _resolved()
        checks.append(Check(
            "zellij-managed", "ok",
            f"zellij {format_version(version)} from {_source(binary)}",
        ))
        return checks
    if zellij_download.platform_key() is None:
        checks.append(Check(
            "zellij-managed", "info",
            f"No pinned zellij build for this platform; {_unsupported_message()}",
        ))
        return checks
    checks.append(Check(
        "zellij-managed", "info",
        f"zellij {_floor()} or newer is not available; tongs-agents can install the "
        "pinned official build into its state directory (downloads from GitHub)",
        FIX_COMMAND,
    ))
    return checks
