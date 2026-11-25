"""A real Spotify search with the saved login. Skipped unless SPOTIFY_CLIENT_ID is set and
`python -m desk_buddy.spotify_login` has been run. Search only: nothing plays."""

import pytest

from desk_buddy.config import ConfigError, load_settings
from desk_buddy.tools.spotify import create_control, logged_in, token_cache_path

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def control():
    try:
        settings = load_settings()
    except ConfigError as exc:
        pytest.skip(f"settings: {exc}")
    if not settings.spotify_client_id or not logged_in(token_cache_path()):
        pytest.skip("no Spotify login (run: uv run python -m desk_buddy.spotify_login)")
    made = create_control(settings.spotify_client_id)
    assert made is not None
    return made


def test_a_real_search_finds_queen(control):
    result = control.call("search", q="Queen", type="artist", limit=1)
    items = result["artists"]["items"]
    assert items and items[0]["uri"].startswith("spotify:artist:")
