"""The one check every web address CARL may open goes through (bookmarks, web search
results and skills' pages): http or https, a host, no user name or password, at most
MAX_URL characters, and nothing invisible or ambiguous in it. Also the search results
(and skills' pages) CARL may open."""

from __future__ import annotations

import ipaddress
import re
import threading
import unicodedata
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlsplit

from desk_buddy.tools.untrusted import wrap

MAX_URL = 500
SCHEMES = ("http", "https")
_PLAIN_HOST = re.compile(r"[a-z0-9.-]+")


MAX_LISTED = 10  # pages from the last question's skills and searches, numbered for Claude
LIST_NOTE_START = (
    "(System note: pages from your last search or skill, for open_search_result by number:"
)
LIST_SOURCE = "search_and_skill_pages"
NOT_AVAILABLE = "(not available)"

# One search result (or a skill's page): (title, site, full address). A skill's page that
# was dropped keeps its number as a placeholder, GAP: listed, but never opened.
Page = tuple[str, str, str]
GAP: Page = ("", "", "")

# Bidi controls: shown text from the web must not flip the dashboard's text direction.
_BIDI = dict.fromkeys(
    [*range(0x202A, 0x202F), *range(0x2066, 0x206A)],
    " ",
)


def plain_line(text: str) -> str:
    """One line of plain text: bidi controls and every line break (including U+0085,
    U+2028 and U+2029, which split() counts as whitespace) become single spaces, and
    other control and format characters (direction marks, zero-width characters, BOMs)
    are dropped. How search results' and skills' page titles are cleaned."""
    text = text.translate(_BIDI)
    kept = "".join(c for c in text if c.isspace() or unicodedata.category(c) not in ("Cc", "Cf"))
    return " ".join(kept.split())


def _stamp() -> int | None:
    """The question being answered now (confirm.QUESTION, set by the loop per turn and
    inherited by tool threads)."""
    from desk_buddy.tools.confirm import QUESTION

    return QUESTION.get()


class SearchResults:
    """CARL's own web search results, and the pages skills hand back: the only pages
    open_search_result may open. Kept on the server side (the dashboard never gets the
    addresses).

    - This question's results can be opened by their exact address (Claude saw a search's
      addresses in this turn).
    - At the next question with speech they become a numbered list (up to MAX_LISTED):
      Claude is shown the titles and sites in a system note (note()) and opens one by its
      number. Numbers only ever mean the latest list.
    - Skills' pages come first, each skill's as one block in the order the skill gave
      them: a dropped page keeps its number as a placeholder (GAP), so the numbers a
      skill's text says are the numbers that open. Then the searches' results, one per
      address, minus any a skill already listed. Past MAX_LISTED, search results go first.
    - Each add is stamped with its question (confirm.QUESTION): one from a question that
      has already moved on (a late tool thread) is dropped.
    - `offered`: whether open_search_result is offered; without it there's no note.

    Thread-safe: searches are added on the event loop, tools resolve from worker threads.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._skills: list[Page] = []  # this question's skill pages (with GAPs)
        self._now: list[Page] = []  # this question's search results
        self._listed: list[Page] = []
        self._question: int | None = None
        self.offered = True

    def new_question(self, question: int | None = None) -> None:
        """A new question with speech (its number, from the loop): this question's pages
        become the numbered list."""
        with self._lock:
            listed = self._skills[:MAX_LISTED]
            taken = {url for _, _, url in listed if url}
            for title, site, url in self._now:
                if len(listed) >= MAX_LISTED:
                    break
                if url not in taken:
                    taken.add(url)
                    listed.append((title, site, url))
            self._listed, self._skills, self._now = listed, [], []
            self._question = question

    def clear(self) -> None:
        """Forget every result (a forget command)."""
        with self._lock:
            self._listed, self._skills, self._now = [], [], []

    def _stale(self, question: int | None) -> bool:
        return self._question is not None and question is not None and question != self._question

    def add(self, pages: Iterable[Page | str], question: int | None = None) -> None:
        """Results of a search in this question, after the ones already here: (title,
        site, address) each, or just an address. An address already here is kept once,
        where it first came. `question` defaults to the current one (confirm.QUESTION)."""
        question = _stamp() if question is None else question
        with self._lock:
            if self._stale(question):
                return
            seen = {url for _, _, url in self._now}
            for page in pages:
                title, site, url = ("", site_name(page), page) if isinstance(page, str) else page
                if isinstance(url, str) and url and url not in seen:
                    seen.add(url)
                    self._now.append((str(title), str(site), url))

    def add_skill_pages(self, pages: Iterable[Page], question: int | None = None) -> int | None:
        """A skill's pages in this question, as one block after any earlier skill's.
        They are checked already; an empty address (GAP) keeps a dropped page's number,
        and so does an address an earlier skill listed. Returns the number its first page
        will have in the next list, or None if the add is stale (its question has moved
        on). `question` defaults to the current one (confirm.QUESTION)."""
        question = _stamp() if question is None else question
        with self._lock:
            if self._stale(question):
                return None
            start = len(self._skills) + 1
            seen = {url for _, _, url in self._skills if url}
            for title, site, url in pages:
                if not url or url in seen:
                    self._skills.append(GAP)
                else:
                    seen.add(url)
                    self._skills.append((str(title), str(site), url))
            return start

    def resolve(self, page: object) -> str | None:
        """The address `page` means: a number from the latest list ("3"), or the exact
        address of a result from this question's searches or skills. None if it's neither
        (or the number is a dropped page's)."""
        text = str(page).strip()
        with self._lock:
            if text.isdigit() and text.isascii():
                n = int(text)
                return (self._listed[n - 1][2] or None) if 1 <= n <= len(self._listed) else None
            if not text:
                return None
            return text if any(url == text for _, _, url in (*self._skills, *self._now)) else None

    def note(self) -> str | None:
        """The numbered list for Claude, as a system note around an untrusted block (the
        titles are web text), or None when the last question found nothing (or
        open_search_result isn't offered)."""
        with self._lock:
            listed = list(self._listed)
        if not listed or not self.offered:
            return None
        lines = [
            f"{i}. {NOT_AVAILABLE}"
            if not url
            else f"{i}. {title or 'Untitled'}" + (f" ({site})" if site else "")
            for i, (title, site, url) in enumerate(listed, 1)
        ]
        return f"{LIST_NOTE_START} {wrap(LIST_SOURCE, chr(10).join(lines))})"

    def __contains__(self, url: object) -> bool:
        """Whether an address is among the results that can still be opened."""
        if not url:
            return False
        with self._lock:
            return any(u == url for _, _, u in (*self._skills, *self._now, *self._listed))


def site_name(url: str) -> str:
    """The site to name aloud ("bbc.co.uk"), or "" if the host is anything but plain
    letters, digits, dots and hyphens (it goes into notes Claude reads)."""
    try:
        host = (urlsplit(url).hostname or "").removeprefix("www.")
    except ValueError:
        return ""
    return host if _PLAIN_HOST.fullmatch(host) else ""


def page_description(url: str) -> str:
    """How to name a page aloud: "the page on bbc.co.uk", or just "the page"."""
    site = site_name(url)
    return f"the page on {site}" if site else "the page"


_LOCAL_SUFFIXES = (".local", ".localhost", ".internal", ".lan", ".home.arpa")
_NUMERIC_HOST = re.compile(r"[0-9.]+|0x[0-9a-fx.]+")


def is_public_host(url: str) -> bool:
    """Whether the address's host is on the public internet as far as we can tell: not
    this computer or the local network. Refused: localhost and single-label names,
    .local/.localhost (and similar) names, and loopback, private, link-local, reserved,
    multicast or unspecified IP addresses (including short numeric forms like 127.1)."""
    try:
        host = (urlsplit(url).hostname or "").rstrip(".").lower()
    except ValueError:
        return False
    if not host or host == "localhost" or host.endswith(_LOCAL_SUFFIXES):
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        if _NUMERIC_HOST.fullmatch(host):
            return False  # a number a browser would still read as an IP address
        return "." in host  # a single-label name is a local one
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped  # ::ffff:127.0.0.1 is 127.0.0.1
    return not (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def check_url(url: Any) -> str:
    """The address, unchanged, if it may be opened. Raises ValueError (a sentence the
    dashboard can show) if not."""
    if not isinstance(url, str) or not url:
        raise ValueError("The address can't be empty.")
    if len(url) > MAX_URL:
        raise ValueError(f"The address can be at most {MAX_URL} characters.")
    # Control and format characters (direction marks too), and any kind of space.
    if any(unicodedata.category(c)[0] in "CZ" for c in url):
        raise ValueError("The address can't contain spaces or invisible characters.")
    if "\\" in url:  # browsers read \ as /, which can move the host
        raise ValueError("The address can't contain a backslash.")
    try:
        parts = urlsplit(url)
        port_ok = parts.port is None or 0 < parts.port
    except ValueError:
        raise ValueError("That isn't a valid web address.") from None
    if parts.scheme.lower() not in SCHEMES:
        raise ValueError("The address must start with https:// or http://.")
    if "@" in parts.netloc or parts.username is not None or parts.password is not None:
        raise ValueError("The address can't contain a user name or password.")
    if not parts.hostname or not port_ok:
        raise ValueError("The address needs a host, like https://example.com.")
    return url
