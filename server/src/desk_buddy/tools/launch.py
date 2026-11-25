"""open_app and open_website: the allowlisted apps and bookmarks from the dashboard's Apps
tab (apps_store).

The model only ever picks a spoken name from an enum; the server looks up what it means
and builds the launch itself:

- an app: explorer.exe with exactly "shell:AppsFolder\\<app ID>", an argument list, no
  shell, no other arguments;
- a website: os.startfile(<bookmarked address>) (the default browser), on its own thread
  with a timeout, and only after check_url passes again.

Every call goes through the ActionGuard (computer control, the limits, the log line, the
dashboard event). Neither needs a spoken "yes".
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
from collections.abc import Callable, Iterable
from typing import Any

from desk_buddy.apps_store import AppEntry, Bookmark, check_app_id
from desk_buddy.tools.registry import Tool, ToolError
from desk_buddy.tools.urls import check_url

log = logging.getLogger(__name__)

OPEN_WAIT_S = 2.0  # wait this long for opening a page to fail at once; never longer

UNKNOWN_APP = "That isn't one of the apps I may open."
UNKNOWN_SITE = "That isn't one of the websites I may open."
APP_FAILED = "I couldn't open {title}."
SITE_FAILED = "I couldn't open {name}."
BAD_SITE = "That website's address isn't allowed."


def launch_app(app_id: str) -> None:
    """Start an app by its Start-menu ID: explorer.exe with one fixed-form argument."""
    subprocess.Popen(["explorer.exe", "shell:AppsFolder\\" + app_id])


def open_url(url: str) -> None:
    """Open a checked address in the default browser (Windows only)."""
    os.startfile(url)  # type: ignore[attr-defined]


def _spoken(name: Any) -> str:
    return " ".join(str(name).split()).lower()


class LaunchTools:
    """open_app and open_website over a snapshot of the lists (rebuilt with the chat
    when the lists change). Each tool exists only when its list isn't empty."""

    def __init__(
        self,
        guard: Any,
        apps: Iterable[AppEntry] = (),
        bookmarks: Iterable[Bookmark] = (),
        *,
        launcher: Callable[[str], None] = launch_app,
        opener: Callable[[str], None] = open_url,
        open_wait: float = OPEN_WAIT_S,
    ) -> None:
        self._guard = guard
        self._launcher = launcher
        self._opener = opener
        self._open_wait = open_wait
        self._apps: dict[str, AppEntry] = {}
        for app in apps:
            for name in app.names:
                self._apps.setdefault(name, app)
        self._sites: dict[str, Bookmark] = {}
        for site in bookmarks:
            self._sites.setdefault(site.name, site)

    @property
    def app_names(self) -> list[str]:
        return list(self._apps)

    @property
    def site_names(self) -> list[str]:
        return list(self._sites)

    # --- the tool functions (their signatures are the tool inputs) ---

    def open_app(self, name: str) -> str:
        app = self._apps.get(_spoken(name))
        if app is None:
            raise ToolError(UNKNOWN_APP)
        return self._guard.run("open_app", {"name": _spoken(name)}, lambda: self._launch(app))

    def open_website(self, name: str) -> str:
        site = self._sites.get(_spoken(name))
        if site is None:
            raise ToolError(UNKNOWN_SITE)
        return self._guard.run("open_website", {"name": site.name}, lambda: self._open(site))

    def tools(self) -> list[Tool]:
        tools: list[Tool] = []
        if self._apps:
            tools.append(
                Tool(
                    name="open_app",
                    description=(
                        "Open an app on the user's computer. name is one of the names the "
                        "user gave their apps; pick the one they asked for."
                    ),
                    input_schema=_name_schema(self.app_names, "Which app."),
                    func=self.open_app,
                )
            )
        if self._sites:
            tools.append(
                Tool(
                    name="open_website",
                    description=(
                        "Open one of the user's bookmarked websites in their browser. name "
                        "is one of the names the user gave their bookmarks."
                    ),
                    input_schema=_name_schema(self.site_names, "Which website."),
                    func=self.open_website,
                )
            )
        return tools

    # --- the actions (run inside the guard, in a worker thread) ---

    def _launch(self, app: AppEntry) -> str:
        try:
            self._launcher(check_app_id(app.app_id))  # again, right before it's started
        except Exception as exc:  # any failure to start it, not only OSError
            log.warning("Couldn't open %s: %s", app.title, exc)
            raise ToolError(APP_FAILED.format(title=app.title)) from exc
        return f"Opened {app.title}."

    def _open(self, site: Bookmark) -> str:
        try:
            url = check_url(site.url)  # again, right before it's opened
        except ValueError as exc:
            raise ToolError(BAD_SITE) from exc
        failed: list[Exception] = []

        def open_it() -> None:
            try:
                self._opener(url)
            except Exception as exc:
                failed.append(exc)

        # os.startfile can block while Windows starts the browser: wait only long enough
        # to report a failure that comes at once.
        thread = threading.Thread(target=open_it, name="open-website", daemon=True)
        thread.start()
        thread.join(self._open_wait)
        if failed:
            log.warning("Couldn't open %s: %s", site.name, failed[0])
            raise ToolError(SITE_FAILED.format(name=site.name)) from failed[0]
        return f"Opened {site.name}."


def _name_schema(names: list[str], description: str) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {"name": {"type": "string", "enum": names, "description": description}},
        "required": ["name"],
        "additionalProperties": False,
    }
