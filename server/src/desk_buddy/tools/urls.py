"""The one check every web address CARL may open goes through (bookmarks now, web search
results later): http or https, a host, no user name or password, at most MAX_URL
characters, and nothing invisible or ambiguous in it."""

from __future__ import annotations

import ipaddress
import re
import threading
import unicodedata
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlsplit

MAX_URL = 500
SCHEMES = ("http", "https")
_PLAIN_HOST = re.compile(r"[a-z0-9.-]+")


class SearchResults:
    """The full addresses of CARL's own web search results, for this question and the one
    before: the only pages open_search_result may open. Kept on the server side only
    (the dashboard never gets them). Thread-safe: searches are added on the event loop,
    tools check them from worker threads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._now: set[str] = set()
        self._before: set[str] = set()

    def new_question(self) -> None:
        with self._lock:
            self._before, self._now = self._now, set()

    def clear(self) -> None:
        """Forget every result (a forget command)."""
        with self._lock:
            self._before, self._now = set(), set()

    def add(self, urls: Iterable[str]) -> None:
        with self._lock:
            self._now.update(u for u in urls if isinstance(u, str) and u)

    def __contains__(self, url: object) -> bool:
        with self._lock:
            return isinstance(url, str) and (url in self._now or url in self._before)


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
