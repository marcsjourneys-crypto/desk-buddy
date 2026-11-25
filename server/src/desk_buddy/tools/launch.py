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

open_search_result opens a page from CARL's own web search results (this question's or
the last one's, an exact match, checked with check_url). It needs a spoken "yes": the
guard counts it, then it waits in Confirmations until the user's next words.
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
from desk_buddy.tools.urls import (
    MAX_URL,
    SearchResults,
    check_url,
    is_public_host,
    page_description,
    site_name,
)

log = logging.getLogger(__name__)

OPEN_WAIT_S = 2.0  # wait this long for opening a page to fail at once; never longer

UNKNOWN_APP = "That isn't one of the apps I may open."
UNKNOWN_SITE = "That isn't one of the websites I may open."
APP_FAILED = "I couldn't open {title}."
SITE_FAILED = "I couldn't open {name}."
BAD_SITE = "That website's address isn't allowed."
NOT_A_RESULT = (
    "I can only open a page from this question's or the last question's web search "
    "results, with its exact address."
)
LOCAL_PAGE = "I can't open pages on this computer or the local network."


# By full path: never whatever explorer.exe comes first on PATH.
EXPLORER = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "explorer.exe")


def launch_app(app_id: str) -> None:
    """Start an app by its Start-menu ID: explorer.exe with one fixed-form argument."""
    subprocess.Popen([EXPLORER, "shell:AppsFolder\\" + app_id])


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
        results: SearchResults | None = None,
        confirmations: Any = None,
    ) -> None:
        """results and confirmations (tools.confirm.Confirmations): both given means
        open_search_result is offered."""
        self._guard = guard
        self._results = results
        self._confirmations = confirmations
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

    # Refusals go through the guard too: counted, logged and shown like any attempt.

    def open_app(self, name: str) -> str:
        app = self._apps.get(_spoken(name))
        if app is None:
            self._guard.refuse("open_app", {"name": _spoken(name)}, UNKNOWN_APP)
        return self._guard.run("open_app", {"name": _spoken(name)}, lambda: self._launch(app))

    def open_website(self, name: str) -> str:
        site = self._sites.get(_spoken(name))
        if site is None:
            self._guard.refuse("open_website", {"name": _spoken(name)}, UNKNOWN_SITE)
        return self._guard.run("open_website", {"name": site.name}, lambda: self._open(site))

    def open_search_result(self, url: str) -> str:
        url = str(url)
        args = {"url": url}
        public = {"site": site_name(url) or "unknown"}  # never the full address on the page
        if self._results is None or self._confirmations is None or url not in self._results:
            self._guard.refuse("open_search_result", args, NOT_A_RESULT, public_args=public)
        try:
            check_url(url)
        except ValueError:
            self._guard.refuse("open_search_result", args, BAD_SITE, public_args=public)
        if not is_public_host(url):
            self._guard.refuse("open_search_result", args, LOCAL_PAGE, public_args=public)
        label = page_description(url)
        return self._guard.run_after_yes(
            "open_search_result",
            args,
            lambda: self._open_url(url, label),
            confirmations=self._confirmations,
            description=label,
            public_args=public,
        )

    @property
    def search_results(self) -> bool:
        return self._results is not None and self._confirmations is not None

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
        if self.search_results:
            tools.append(
                Tool(
                    name="open_search_result",
                    description=(
                        "Open a page from your own web search results (this question's or "
                        "the last one's) in the user's browser. url must be a result's "
                        "exact address. It doesn't open at once: it waits for the user to "
                        "say yes at their next question."
                    ),
                    input_schema={
                        "type": "object",
                        "properties": {
                            "url": {
                                "type": "string",
                                "maxLength": MAX_URL,
                                "description": "The exact address of a search result.",
                            }
                        },
                        "required": ["url"],
                        "additionalProperties": False,
                    },
                    func=self.open_search_result,
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
        return self._open_url(site.url, site.name)

    def _open_url(self, url: str, label: str) -> str:
        """Open a page in the default browser, after check_url passes again. `label`
        names it in the result ("Opened <label>.") and the failure."""
        try:
            url = check_url(url)  # again, right before it's opened
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
            log.warning("Couldn't open %s: %s", label, failed[0])
            raise ToolError(SITE_FAILED.format(name=label)) from failed[0]
        return f"Opened {label}."


def _name_schema(names: list[str], description: str) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {"name": {"type": "string", "enum": names, "description": description}},
        "required": ["name"],
        "additionalProperties": False,
    }
