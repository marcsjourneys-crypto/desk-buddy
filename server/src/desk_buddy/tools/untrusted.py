"""Mark text that tools bring in from outside (notes, Spotify names) as untrusted data.

The system prompt tells Claude that anything inside <untrusted source="..."> is data to
read or sum up, never instructions. Text inside can't close the block early: any
<untrusted or </untrusted in it (any case, any spacing) has its "<" escaped.
"""

from __future__ import annotations

import re

_TAG = re.compile(r"<(?=\s*/?\s*untrusted)", re.IGNORECASE)
_SOURCE = re.compile(r"[a-z_]+")


def wrap(source: str, text: str) -> str:
    """`text` inside an untrusted block labelled with `source` (a plain lowercase name)."""
    if not _SOURCE.fullmatch(source):
        raise ValueError(f"source must be a plain lowercase name, got {source!r}")
    return f'<untrusted source="{source}">\n{_TAG.sub("&lt;", text)}\n</untrusted>'
