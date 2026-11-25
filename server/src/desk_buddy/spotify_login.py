"""Log CARL in to Spotify, once: uv run python -m desk_buddy.spotify_login

Needs SPOTIFY_CLIENT_ID in server/.env (a Spotify developer app with the redirect URI
http://127.0.0.1:8888/callback). Opens the browser for Spotify's consent page, then saves
the token to %LOCALAPPDATA%\\desk-buddy\\spotify_token.json. The token is never printed.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

from spotipy.exceptions import SpotifyBaseException
from spotipy.oauth2 import SpotifyPKCE

from desk_buddy.config import ConfigError, load_settings
from desk_buddy.tools.spotify import REDIRECT_URI, make_auth, token_cache_path


def login_auth(client_id: str, cache_path: Path) -> Any:
    """spotipy's PKCE flow: opens the browser and catches the redirect on 127.0.0.1:8888."""
    return make_auth(SpotifyPKCE, client_id, cache_path, open_browser=True)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Can't start: {exc}", file=sys.stderr)
        return 2
    if not settings.spotify_client_id:
        print(
            "Set SPOTIFY_CLIENT_ID in server/.env first: the Client ID of your Spotify "
            f"developer app, with the redirect URI {REDIRECT_URI}.",
            file=sys.stderr,
        )
        return 2
    cache = token_cache_path()
    cache.parent.mkdir(parents=True, exist_ok=True)
    print("Opening Spotify in your browser. Log in if asked, then click Agree.")
    auth = login_auth(settings.spotify_client_id, cache)
    try:
        auth.get_access_token(check_cache=False)  # always a fresh login; the token stays put
    except (SpotifyBaseException, OSError) as exc:
        print(f"Spotify login failed: {exc}", file=sys.stderr)
        return 1
    print(f"Logged in to Spotify. CARL will remember it (saved in {cache}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
