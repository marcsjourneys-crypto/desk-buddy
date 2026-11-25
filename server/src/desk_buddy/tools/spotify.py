"""Spotify through its Web API (spotipy), for CARL's music tools.

Login is PKCE (no client secret): `python -m desk_buddy.spotify_login` saves a token to
%LOCALAPPDATA%\\desk-buddy\\spotify_token.json. The server only ever reuses and refreshes
it; it never starts an interactive login. Every call here is blocking: tools run in a
worker thread.
"""

from __future__ import annotations

import difflib
import json
import logging
import os
import platform
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path
from typing import Any

import requests
import spotipy
from spotipy.exceptions import SpotifyException, SpotifyOauthError
from spotipy.oauth2 import SpotifyPKCE

from desk_buddy.config import app_data_dir
from desk_buddy.tools.registry import Tool, ToolError
from desk_buddy.tools.untrusted import wrap

log = logging.getLogger(__name__)

# spotipy logs request headers (the bearer token) and token-refresh bodies (the refresh
# token) at DEBUG. Never let those reach our logs.
logging.getLogger("spotipy").setLevel(logging.WARNING)

REDIRECT_URI = "http://127.0.0.1:8888/callback"
SCOPES = (
    "user-read-playback-state user-modify-playback-state user-read-currently-playing "
    "playlist-read-private"
)
REQUEST_TIMEOUT_S = 10  # login token calls (spotipy's PKCE has no timeout by default)
API_TIMEOUT_S = 5  # each Web API request
API_RETRIES = 1  # connection retries
API_STATUS_RETRIES = 0  # never retry on 429/5xx: urllib3 would sleep for any Retry-After
CALL_DEADLINE_S = 12.0  # the most one spotipy call may take, retries and token refresh included
START_WAIT_SECONDS = 8.0
POLL_SECONDS = 0.5
TRANSFER_TRIES = 3  # a just-started Spotify can 404 on transfer for a moment
TRANSFER_RETRY_S = 0.5

# What CARL says (tool error results).
NOT_LOGGED_IN = "Music isn't set up: run the Spotify login."
PREMIUM = "Spotify control needs Premium."
NOT_FOUND = "I couldn't find that one."
NO_START = "Spotify didn't open."
NO_DEVICE = "Spotify isn't open on any device."
NOT_READY = "Spotify isn't ready yet."
NETWORK = "I can't reach Spotify right now."
BUSY = "Spotify is busy, try again in a moment."
REFUSED = "Spotify said no to that."

# spotipy calls run here so each can be given up on at CALL_DEADLINE_S. A call given up on
# finishes in the background and its result is dropped.
_CALLS = ThreadPoolExecutor(max_workers=4, thread_name_prefix="spotify")


class MusicError(ToolError):
    """A Spotify problem, as a short sentence CARL can say."""


def token_cache_path() -> Path:
    return app_data_dir() / "spotify_token.json"


def logged_in(cache_path: str | Path) -> bool:
    """A saved login with a refresh token. Reads the file only; never logs its contents."""
    try:
        token = json.loads(Path(cache_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(token, dict) and bool(token.get("refresh_token"))


class _ServerPKCE(SpotifyPKCE):
    """PKCE that refreshes a saved token but never asks for a new login: in the server, a
    browser window or an input() prompt would hang the turn."""

    def get_authorization_code(self, response: Any = None) -> str:
        raise MusicError(NOT_LOGGED_IN)


def make_auth(cls: type[SpotifyPKCE], client_id: str, cache_path: str | Path, **kwargs: Any) -> Any:
    """A PKCE auth manager (cls: SpotifyPKCE or a subclass) with CARL's settings."""
    from spotipy.cache_handler import CacheFileHandler

    return cls(
        client_id=client_id,
        redirect_uri=REDIRECT_URI,
        scope=SCOPES,
        requests_timeout=REQUEST_TIMEOUT_S,
        cache_handler=CacheFileHandler(cache_path=str(cache_path)),
        **kwargs,
    )


def server_auth(client_id: str, cache_path: str | Path) -> SpotifyPKCE:
    return make_auth(_ServerPKCE, client_id, cache_path, open_browser=False)


def create_control(
    client_id: str,
    cache_path: str | Path | None = None,
    *,
    timeout: float = API_TIMEOUT_S,
    retries: int = API_RETRIES,
) -> SpotifyControl | None:
    """A SpotifyControl, or None if music isn't set up (no client ID or no saved login)."""
    cache_path = token_cache_path() if cache_path is None else cache_path
    if not client_id or not logged_in(cache_path):
        return None
    client = spotipy.Spotify(
        auth_manager=server_auth(client_id, cache_path),
        requests_timeout=timeout,
        retries=retries,
        status_retries=API_STATUS_RETRIES,
    )
    return SpotifyControl(client)


def start_spotify() -> None:
    """Open the Spotify app: a fixed URI handed to Windows, no shell, no model text."""
    os.startfile("spotify:")  # type: ignore[attr-defined]  # Windows only


class SpotifyControl:
    """A spotipy client (injectable for tests) plus finding a device to play on."""

    def __init__(
        self,
        client: Any,
        *,
        pc_name: str | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        start_app: Callable[[], None] = start_spotify,
    ) -> None:
        self.client = client
        self._pc_name = (pc_name or platform.node()).lower()
        self._clock = clock
        self._sleep = sleep
        self._start_app = start_app

    def call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """Call a spotipy method, turning Spotify errors into a MusicError CARL can say.
        Gives up after CALL_DEADLINE_S whatever spotipy is doing."""
        future = _CALLS.submit(getattr(self.client, method), *args, **kwargs)
        try:
            return future.result(timeout=CALL_DEADLINE_S)
        except FutureTimeout as exc:
            log.warning("Spotify %s took over %.0f s: gave up", method, CALL_DEADLINE_S)
            raise MusicError(NETWORK) from exc
        except MusicError:
            raise
        except SpotifyOauthError as exc:
            log.warning("Spotify login failed: %s", type(exc).__name__)
            raise MusicError(NOT_LOGGED_IN) from exc
        except SpotifyException as exc:
            raise MusicError(_message(exc)) from exc
        except requests.exceptions.RequestException as exc:
            log.warning("Spotify unreachable: %s", type(exc).__name__)
            raise MusicError(NETWORK) from exc

    def ensure_device(self) -> str:
        """The device to play on, made active: this PC's Spotify, else the active device,
        else any computer running Spotify (its name can differ from the PC's), else start
        Spotify here and wait up to START_WAIT_SECONDS for it. Devices without an id, or
        restricted ones (they can't be controlled), are ignored."""
        devices = self._devices()
        device = (
            self._this_pc(devices)
            or next((d for d in devices if d.get("is_active")), None)
            or self._a_computer(devices)
        )
        if device is None:
            device = self._start_and_wait()
        if not device.get("is_active"):
            # Without an active device, pause/next/volume fail; don't start music yet.
            self._activate(device["id"])
        return device["id"]

    # --- internals ---

    def _devices(self) -> list[dict[str, Any]]:
        devices = (self.call("devices") or {}).get("devices") or []
        return [d for d in devices if d and d.get("id") and not d.get("is_restricted")]

    def _this_pc(self, devices: list[dict[str, Any]]) -> dict[str, Any] | None:
        return next(
            (
                d
                for d in devices
                if d.get("type") == "Computer" and str(d.get("name", "")).lower() == self._pc_name
            ),
            None,
        )

    def _a_computer(self, devices: list[dict[str, Any]]) -> dict[str, Any] | None:
        return next((d for d in devices if d.get("type") == "Computer"), None)

    def _activate(self, device_id: str) -> None:
        for attempt in range(TRANSFER_TRIES):
            try:
                self.call("transfer_playback", device_id, force_play=False)
                return
            except MusicError as exc:
                cause = exc.__cause__
                if not (isinstance(cause, SpotifyException) and cause.http_status == 404):
                    raise
                if attempt == TRANSFER_TRIES - 1:
                    raise MusicError(NOT_READY) from exc
                self._sleep(TRANSFER_RETRY_S)

    def _start_and_wait(self) -> dict[str, Any]:
        log.info("Starting Spotify")
        try:
            self._start_app()
        except OSError as exc:
            log.warning("Couldn't start Spotify: %s", exc)
            raise MusicError(NO_START) from exc
        deadline = self._clock() + START_WAIT_SECONDS
        while self._clock() < deadline:
            self._sleep(POLL_SECONDS)
            devices = self._devices()
            device = self._this_pc(devices) or self._a_computer(devices)
            if device is not None:
                return device
        raise MusicError(NO_START)


def _message(exc: SpotifyException) -> str:
    reason = exc.reason or ""
    log.warning("Spotify error %s %s", exc.http_status, reason)
    if reason == "PREMIUM_REQUIRED":
        return PREMIUM
    if exc.http_status == 401:
        return NOT_LOGGED_IN
    if reason == "NO_ACTIVE_DEVICE":
        return NO_DEVICE
    if exc.http_status == 404:
        return NOT_FOUND
    if exc.http_status == 429:
        return BUSY
    if exc.http_status in (400, 403):  # e.g. pausing what's already paused
        return REFUSED
    return NETWORK


# --- The music tools ---

KINDS = ("track", "artist", "album", "playlist")
ACTIONS = ("pause", "resume", "next", "previous")
MAX_QUERY = 200
SEARCH_LIMIT = 5
PLAYLIST_PAGE = 50
PLAYLIST_PAGES = 4  # up to 200 of the user's own playlists
PLAYLIST_MATCH = 0.6  # difflib ratio for a fuzzy playlist-name match
SOURCE = "spotify"

_CONTROL_DONE = {
    "pause": "Paused.",
    "resume": "Resumed.",
    "next": "Skipped to the next track.",
    "previous": "Back to the previous track.",
}


def _artists(item: dict[str, Any]) -> str:
    return ", ".join(a.get("name", "") for a in item.get("artists") or [] if a)


def _describe(kind: str, item: dict[str, Any]) -> str:
    name = item.get("name", "")
    if kind == "track":
        return f"{name} by {_artists(item)}"
    if kind == "album":
        return f"the album {name} by {_artists(item)}"
    if kind == "playlist":
        return f"the playlist {name}"
    return name


class MusicTools:
    """play_music, music_control, music_volume and now_playing. Every call goes through
    the ActionGuard; names that come from Spotify are wrapped as untrusted. Model text is
    only ever a search query; playback uses the URIs Spotify returned."""

    def __init__(self, control: SpotifyControl, guard: Any, ducker: Any = None) -> None:
        self._control = control
        self._guard = guard
        self._ducker = ducker  # a MusicDucker, so a volume set while ducked sticks

    # --- the tool functions (their signatures are the tool inputs) ---

    def play_music(self, query: str, kind: str) -> str:
        query = " ".join(str(query).split())
        if not query or len(query) > MAX_QUERY:
            raise ToolError(f"query must be 1 to {MAX_QUERY} characters.")
        if kind not in KINDS:
            raise ToolError(f"kind must be one of {', '.join(KINDS)}.")
        args = {"query": query, "kind": kind}
        return self._guard.run("play_music", args, lambda: self._play(query, kind))

    def music_control(self, action: str) -> str:
        if action not in ACTIONS:
            raise ToolError(f"action must be one of {', '.join(ACTIONS)}.")
        return self._guard.run("music_control", {"action": action}, lambda: self._do(action))

    def music_volume(self, level: int | None, change: int | None) -> str:
        if (level is None) == (change is None):
            raise ToolError("Give exactly one of level or change.")
        if level is not None and not _int_in(level, 0, 100):
            raise ToolError("level must be a whole number from 0 to 100.")
        if change is not None and not _int_in(change, -50, 50):
            raise ToolError("change must be a whole number from -50 to 50.")
        args = {"level": level, "change": change}
        return self._guard.run("music_volume", args, lambda: self._volume(level, change))

    def now_playing(self) -> str:
        return self._guard.run("now_playing", {}, self._now_playing)

    def tools(self) -> list[Tool]:
        return [
            Tool(
                name="play_music",
                description=(
                    "Play music on Spotify. query is what to search for, in the user's words "
                    "(at most 200 characters). kind: track for a song, artist to play an "
                    "artist, album for an album, playlist for a playlist (the user's own "
                    "playlists are checked first)."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "What to search for."},
                        "kind": {"type": "string", "enum": list(KINDS)},
                    },
                    "required": ["query", "kind"],
                    "additionalProperties": False,
                },
                func=self.play_music,
            ),
            Tool(
                name="music_control",
                description="Pause, resume, skip to the next track or go back on Spotify.",
                input_schema={
                    "type": "object",
                    "properties": {"action": {"type": "string", "enum": list(ACTIONS)}},
                    "required": ["action"],
                    "additionalProperties": False,
                },
                func=self.music_control,
            ),
            Tool(
                name="music_volume",
                description=(
                    "Set Spotify's volume. Give exactly one: level, a whole number 0 to 100, "
                    "or change, a step from -50 to 50 (for example -20 for 'a bit quieter'). "
                    "The other one is null."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "level": {"type": ["integer", "null"], "description": "0 to 100."},
                        "change": {"type": ["integer", "null"], "description": "-50 to 50."},
                    },
                    "required": ["level", "change"],
                    "additionalProperties": False,
                },
                func=self.music_volume,
            ),
            Tool(
                name="now_playing",
                description="What's playing on Spotify: the track, the artist, and whether "
                "it's playing or paused.",
                input_schema={
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False,
                },
                func=self.now_playing,
            ),
        ]

    # --- the actions (run inside the guard, in a worker thread) ---

    def _play(self, query: str, kind: str) -> str:
        item = self._own_playlist(query) if kind == "playlist" else None
        item = item or self._search(query, kind)
        if item is None:
            raise MusicError(NOT_FOUND)
        device = self._control.ensure_device()  # only once there's something to play
        if kind == "track":
            self._control.call("start_playback", device_id=device, uris=[item["uri"]])
        else:
            self._control.call("start_playback", device_id=device, context_uri=item["uri"])
        return "Started playing on Spotify: " + wrap(SOURCE, _describe(kind, item))

    def _search(self, query: str, kind: str) -> dict[str, Any] | None:
        result = self._control.call("search", q=query, type=kind, limit=SEARCH_LIMIT) or {}
        items = (result.get(f"{kind}s") or {}).get("items") or []
        return next((i for i in items if i and i.get("uri")), None)

    def _own_playlist(self, query: str) -> dict[str, Any] | None:
        """The best match among the user's own playlists: an exact name, else a name that
        contains the query (or is contained in it), else a close fuzzy match."""
        wanted = query.casefold()
        best: tuple[tuple[bool, bool, float], dict[str, Any]] | None = None
        for page in range(PLAYLIST_PAGES):
            result = self._control.call(
                "current_user_playlists", limit=PLAYLIST_PAGE, offset=page * PLAYLIST_PAGE
            )
            for item in (result or {}).get("items") or []:
                name = str((item or {}).get("name") or "").casefold()
                if not name or not item.get("uri"):
                    continue
                score = (
                    name == wanted,
                    name in wanted or wanted in name,
                    difflib.SequenceMatcher(None, wanted, name).ratio(),
                )
                if best is None or score > best[0]:
                    best = (score, item)
            if not (result or {}).get("next"):
                break
        if best is None:
            return None
        (exact, contained, ratio), item = best
        return item if exact or contained or ratio >= PLAYLIST_MATCH else None

    def _do(self, action: str) -> str:
        if action == "resume":
            self._control.call("start_playback", device_id=self._control.ensure_device())
        else:
            method = {"pause": "pause_playback", "next": "next_track"}.get(action, "previous_track")
            self._control.call(method)
        return _CONTROL_DONE[action]

    def _volume(self, level: int | None, change: int | None) -> str:
        playback = self._control.call("current_playback")
        device = (playback or {}).get("device") or {}
        if not device.get("id"):
            raise MusicError(NO_DEVICE)
        # While CARL talks the music is ducked: work from the level before ducking, and
        # cancel the restore so the user's new level stays.
        before_duck = self._ducker.take_saved() if self._ducker else None
        if level is None:
            current = before_duck if before_duck is not None else device.get("volume_percent")
            level = max(0, min(100, int(current or 0) + int(change or 0)))
        self._control.call("volume", level, device_id=device["id"])
        return f"Spotify volume set to {level} percent."

    def _now_playing(self) -> str:
        playback = self._control.call("current_playback")
        item = (playback or {}).get("item")
        if not item:
            return "Nothing is playing on Spotify right now."
        if item.get("type") == "episode":
            desc = f"{item.get('name', '')} from {(item.get('show') or {}).get('name', '')}"
        else:
            desc = _describe("track", item)
        state = "playing" if playback.get("is_playing") else "paused"
        return f"Spotify is {state}: " + wrap(SOURCE, desc)


def _int_in(value: Any, low: int, high: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high
