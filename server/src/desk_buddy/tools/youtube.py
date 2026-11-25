"""YouTube through the YouTube Data API v3 (plain HTTPS with urllib), for play_youtube.

The API key is a secret: it travels only in the `key` query parameter of a request and
never appears in a log line, an exception message, a repr or a tool result. Every URL is
redacted before it is logged, and HTTP and network errors are turned into our own
YouTubeError (a sentence CARL can say) without chaining urllib's exception, which holds
the full URL.

Every call here is blocking: tools run in a worker thread.
"""

from __future__ import annotations

import html
import json
import logging
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any, NamedTuple
from urllib.parse import urlencode

from desk_buddy.tools.registry import ToolError

log = logging.getLogger(__name__)

API_BASE = "https://www.googleapis.com/youtube/v3/"
WATCH_URL = "https://www.youtube.com/watch?v="
TIMEOUT_S = 10.0  # each request
MAX_BODY = 1_000_000  # bytes of a response we read at most
SEARCH_RESULTS = 5
MAX_TEXT = 150  # characters of a title or channel name we keep

# What CARL says (tool error results).
QUOTA = "YouTube's daily limit is used up."
NOT_SET_UP = "YouTube isn't set up properly (check the API key)."
NETWORK = "I can't reach YouTube right now."
BUSY = "YouTube is busy, try again in a moment."
NOT_FOUND = "I couldn't find that one."

_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")  # always fullmatch
_API_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")  # a channel or playlist ID, loosely
_REASON = re.compile(r"[A-Za-z_]{1,40}")  # an API error reason we may log
_KEY_PARAM = re.compile(r"([?&]key=)[^&#]*")
_QUOTA_REASONS = frozenset({"quotaExceeded", "dailyLimitExceeded"})
_BUSY_REASONS = frozenset({"rateLimitExceeded", "userRateLimitExceeded"})

# The HTTP function: (url, timeout) -> the response body. Raises urllib.error.HTTPError
# for an HTTP error status, and URLError or OSError (timeouts too) for a network failure.
HttpGet = Callable[[str, float], bytes]


class YouTubeError(ToolError):
    """A YouTube problem, as a short sentence CARL can say. Never holds the key."""


class Video(NamedTuple):
    video_id: str
    title: str
    channel: str


def urllib_get(url: str, timeout: float) -> bytes:
    """GET over HTTPS with urllib (the real HTTP function)."""
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read(MAX_BODY)


def redact(url: str) -> str:
    """The URL with the value of its `key` parameter hidden: the only form ever logged."""
    return _KEY_PARAM.sub(r"\1REDACTED", url)


def is_video_id(value: Any) -> bool:
    return isinstance(value, str) and _VIDEO_ID.fullmatch(value) is not None


def watch_url(video_id: str) -> str:
    """The watch page of a video, built here from a checked ID. Raises ValueError."""
    if not is_video_id(video_id):
        raise ValueError("not a YouTube video ID")
    return WATCH_URL + video_id


def _text(value: Any) -> str:
    """A title or channel name as plain text: the search API sends HTML entities
    ("&#39;"), which are decoded here (wrap() escapes what it must again); whitespace is
    collapsed and the length capped."""
    text = " ".join(html.unescape(str(value or "")).split())
    return text[:MAX_TEXT]


class YouTubeClient:
    """The few Data API calls play_youtube needs, with an injectable HTTP function."""

    def __init__(self, api_key: str, *, http: HttpGet = urllib_get, timeout: float = TIMEOUT_S):
        if not api_key:
            raise ValueError("a YouTube API key is needed")
        self.__key = api_key
        self._http = http
        self._timeout = timeout

    def __repr__(self) -> str:
        return "YouTubeClient(key=<hidden>)"

    def search_video(self, query: str) -> Video | None:
        """The best matching video (search.list, 100 quota units), or None."""
        data = self._get(
            "search",
            part="snippet",
            type="video",
            maxResults=SEARCH_RESULTS,
            q=query,
            safeSearch="moderate",
        )
        for item in _items(data):
            video_id = (item.get("id") or {}).get("videoId") if isinstance(item, dict) else None
            if is_video_id(video_id):
                snippet = item.get("snippet") or {}
                return Video(
                    video_id, _text(snippet.get("title")), _text(snippet.get("channelTitle"))
                )
        return None

    def latest_from_channel(self, query: str) -> Video | None:
        """The newest upload of the best matching channel, or None: search.list for the
        channel (100 units), channels.list for its uploads playlist (1 unit), then
        playlistItems.list for the newest item (1 unit)."""
        found = _items(self._get("search", part="snippet", type="channel", maxResults=1, q=query))
        channel = found[0] if found and isinstance(found[0], dict) else {}
        channel_id = (channel.get("id") or {}).get("channelId")
        if not _is_api_id(channel_id):
            return None
        details = _items(self._get("channels", part="contentDetails", id=channel_id))
        first = details[0] if details and isinstance(details[0], dict) else {}
        uploads = ((first.get("contentDetails") or {}).get("relatedPlaylists") or {}).get("uploads")
        if not _is_api_id(uploads):
            return None
        latest = _items(
            self._get("playlistItems", part="snippet", playlistId=uploads, maxResults=1)
        )
        item = latest[0] if latest and isinstance(latest[0], dict) else {}
        snippet = item.get("snippet") or {}
        video_id = (snippet.get("resourceId") or {}).get("videoId")
        if not is_video_id(video_id):
            return None
        channel_name = snippet.get("videoOwnerChannelTitle") or snippet.get("channelTitle")
        if not channel_name:
            channel_name = (channel.get("snippet") or {}).get("channelTitle")
        return Video(video_id, _text(snippet.get("title")), _text(channel_name))

    # --- internals ---

    def _get(self, endpoint: str, **params: Any) -> dict[str, Any]:
        url = API_BASE + endpoint + "?" + urlencode({**params, "key": self.__key})
        log.debug("YouTube GET %s", redact(url))
        try:
            body = self._http(url, self._timeout)
        except urllib.error.HTTPError as exc:
            # Never chain or log exc itself: it holds the URL, and so the key.
            message = _http_message(endpoint, exc.code, _error_body(exc))
            raise YouTubeError(message) from None
        except (urllib.error.URLError, OSError) as exc:  # URLError first: it's an OSError
            log.warning("YouTube %s unreachable: %s", endpoint, type(exc).__name__)
            raise YouTubeError(NETWORK) from None
        try:
            data = json.loads(body)
        except (TypeError, ValueError):
            log.warning("YouTube %s sent something that isn't JSON", endpoint)
            raise YouTubeError(NETWORK) from None
        if not isinstance(data, dict):
            raise YouTubeError(NETWORK)
        return data


def _items(data: dict[str, Any]) -> list[Any]:
    items = data.get("items")
    return items if isinstance(items, list) else []


def _is_api_id(value: Any) -> bool:
    return isinstance(value, str) and _API_ID.fullmatch(value) is not None


def _error_body(exc: urllib.error.HTTPError) -> Any:
    try:
        return json.loads(exc.read(MAX_BODY))
    except Exception:
        return None


def _reasons(body: Any) -> set[str]:
    """The error reasons in a Data API error body (errors[].reason and details[].reason),
    only those that look like plain reason names."""
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return set()
    found = set()
    for key in ("errors", "details"):
        for entry in error.get(key) or []:
            reason = entry.get("reason") if isinstance(entry, dict) else None
            if isinstance(reason, str) and _REASON.fullmatch(reason):
                found.add(reason)
    return found


def _http_message(endpoint: str, status: int, body: Any) -> str:
    reasons = _reasons(body)
    log.warning("YouTube %s error %s %s", endpoint, status, ",".join(sorted(reasons)) or "-")
    if status == 403 and reasons & _QUOTA_REASONS:
        return QUOTA
    if status == 429 or reasons & _BUSY_REASONS:
        return BUSY
    if status in (400, 401, 403):  # keyInvalid, accessNotConfigured, a bad request
        return NOT_SET_UP
    if status == 404:
        return NOT_FOUND
    return NETWORK
