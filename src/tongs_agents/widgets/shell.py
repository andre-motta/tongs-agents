"""The shared app shell of the monitor and the sidebar: header, footer and theme."""

from __future__ import annotations

from textual.app import App
from textual.widgets import Footer, Header

from tongs_agents.app_config import DEFAULT_THEME


class AppHeader(Header):
    """The header both apps show: title, sub-title and the palette button."""


class AppFooter(Footer):
    """The footer both apps show: bindings and the palette key."""


def apply_theme(app: App, name: str) -> str | None:
    """Switch ``app`` to theme ``name``; return a warning when it is not available.

    An unknown name keeps the current theme, because the theme set differs
    between Textual releases and the app should still start.
    """
    if name in app.available_themes:
        app.theme = name
        return None
    return f"unknown theme {name!r:.70}; using {app.theme or DEFAULT_THEME!r}"
