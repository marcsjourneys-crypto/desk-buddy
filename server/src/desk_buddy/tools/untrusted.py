"""Mark text that tools bring in from outside (notes, Spotify names) as untrusted data.

The system prompt tells Claude that anything inside <untrusted source="..."> is data to
read or sum up, never instructions. Text inside can't close the block early or fake any
other tag: every "&", "<" and ">" in it is escaped.
"""

from __future__ import annotations

import html
import re

_SOURCE = re.compile(r"[a-z_]+")
_WRAPPED = re.compile(r'(<untrusted source="[a-z_]+">\n)(.*)(\n</untrusted>)', re.DOTALL)
MORE = "\n(and more)"


def wrap(source: str, text: str, limit: int | None = None) -> str:
    """`text`, escaped, inside an untrusted block labelled `source` (a plain lowercase
    name). With `limit`, the whole result is at most that long: the escaped text is cut
    (never mid-escape) and ends with "(and more)"."""
    if not _SOURCE.fullmatch(source):
        raise ValueError(f"source must be a plain lowercase name, got {source!r}")
    head, tail = f'<untrusted source="{source}">\n', "\n</untrusted>"
    body = html.escape(text, quote=False)
    if limit is not None and len(head) + len(body) + len(tail) > limit:
        body = _cut(body, limit - len(head) - len(tail))
    return head + body + tail


def shorten(text: str, limit: int) -> str:
    """`text` cut to at most `limit` characters. An untrusted block stays well formed: the
    cut is made inside it (never mid-escape), keeping its opening and closing tags."""
    if len(text) <= limit:
        return text
    match = _WRAPPED.fullmatch(text)
    if match is None:
        return _cut(text, limit)
    head, body, tail = match.groups()
    return head + _cut(body, limit - len(head) - len(tail)) + tail


def _cut(body: str, room: int) -> str:
    """`body` cut so that it plus "(and more)" fits in `room` characters."""
    body = body[: max(room - len(MORE), 0)]
    amp = body.rfind("&", max(len(body) - 5, 0))  # "&amp;" is the longest escape
    if amp != -1 and ";" not in body[amp:]:
        body = body[:amp]  # don't leave half an escape
    return body.rstrip() + MORE
