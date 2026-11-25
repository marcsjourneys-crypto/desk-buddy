"""Spotify through its Web API (spotipy), for CARL's music tools.

Login is PKCE (no client secret): `python -m desk_buddy.spotify_login` saves a token to
%LOCALAPPDATA%\\desk-buddy\\spotify_token.json. The server only ever reuses and refreshes
it; it never starts an interactive login. Every call here is blocking: tools run in a
worker thread.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import requests
import spotipy
from spotipy.exceptions import SpotifyException, SpotifyOauthError
from spotipy.oauth2 import SpotifyPKCE

from desk_buddy.config import app_data_dir
from desk_buddy.tools.registry import ToolError

log = logging.getLogger(__name__)

# spotipy logs request headers (the bearer token) and token-refresh bodies (the refresh
# token) at DEBUG. Never let those reach our logs.
logging.getLogger("spotipy").setLevel(logging.WARNING)

REDIRECT_URI = "http://127.0.0.1:8888/callback"
SCOPES = (
    "user-read-playback-state user-modify-playback-state user-read-currently-playing "
    "playlist-read-private"
)
REQUEST_TIMEOUT_S = 10  # spotipy's PKCE token calls have no timeout by default
API_RETRIES = 2
START_WAIT_SECONDS = 8.0
POLL_SECONDS = 0.5

# What CARL says (tool error results).
NOT_LOGGED_IN = "Music isn't set up: run the Spotify login."
PREMIUM = "Spotify control needs Premium."
NOT_FOUND = "I couldn't find that one."
NO_START = "Spotify didn't open."
NO_DEVICE = "Spotify isn't open on any device."
NETWORK = "I can't reach Spotify right now."


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


def create_control(client_id: str, cache_path: str | Path | None = None) -> SpotifyControl | None:
    """A SpotifyControl, or None if music isn't set up (no client ID or no saved login)."""
    cache_path = token_cache_path() if cache_path is None else cache_path
    if not client_id or not logged_in(cache_path):
        return None
    client = spotipy.Spotify(
        auth_manager=server_auth(client_id, cache_path),
        requests_timeout=REQUEST_TIMEOUT_S,
        retries=API_RETRIES,
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
        """Call a spotipy method, turning Spotify errors into a MusicError CARL can say."""
        try:
            return getattr(self.client, method)(*args, **kwargs)
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
        else start Spotify here and wait up to START_WAIT_SECONDS for it."""
        devices = self._devices()
        device = self._this_pc(devices) or next((d for d in devices if d.get("is_active")), None)
        if device is None:
            device = self._start_and_wait({d.get("id") for d in devices})
        if not device.get("is_active"):
            # Without an active device, pause/next/volume fail; don't start music yet.
            self.call("transfer_playback", device["id"], force_play=False)
        return device["id"]

    # --- internals ---

    def _devices(self) -> list[dict[str, Any]]:
        return list((self.call("devices") or {}).get("devices") or [])

    def _this_pc(self, devices: list[dict[str, Any]]) -> dict[str, Any] | None:
        return next(
            (
                d
                for d in devices
                if d.get("type") == "Computer" and str(d.get("name", "")).lower() == self._pc_name
            ),
            None,
        )

    def _start_and_wait(self, known: set[Any]) -> dict[str, Any]:
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
            device = self._this_pc(devices) or next(
                (d for d in devices if d.get("type") == "Computer" and d.get("id") not in known),
                None,
            )
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
    return NETWORK
