"""The one check every web address CARL may open goes through (bookmarks now, web search
results later): http or https, a host, no user name or password, at most MAX_URL
characters, and nothing invisible or ambiguous in it."""

from __future__ import annotations

import unicodedata
from typing import Any
from urllib.parse import urlsplit

MAX_URL = 500
SCHEMES = ("http", "https")


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
