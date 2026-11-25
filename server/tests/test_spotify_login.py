"""python -m desk_buddy.spotify_login, with spotipy's PKCE flow faked (no browser, no
network)."""

import pytest
from spotipy.exceptions import SpotifyOauthError

import desk_buddy.spotify_login as login
from desk_buddy.config import Settings

TOKEN = "BQ-very-secret-access-token"


class FakeAuth:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def get_access_token(self, code=None, check_cache=True):
        self.calls.append(check_cache)
        if self.error:
            raise self.error
        return TOKEN


@pytest.fixture
def setup(monkeypatch, tmp_path):
    cache = tmp_path / "desk-buddy" / "spotify_token.json"
    monkeypatch.setattr(login, "token_cache_path", lambda: cache)
    made = {}

    def fake_login_auth(client_id, cache_path):
        made.update(client_id=client_id, cache_path=cache_path)
        return made.setdefault("auth", FakeAuth())

    monkeypatch.setattr(login, "login_auth", fake_login_auth)
    monkeypatch.setattr(
        login, "load_settings", lambda: Settings(anthropic_api_key="k", spotify_client_id="cid")
    )
    return cache, made


def test_login_runs_the_browser_flow_and_never_shows_the_token(setup, capsys, caplog):
    cache, made = setup
    assert login.main() == 0
    assert made["client_id"] == "cid"
    assert made["cache_path"] == cache
    assert cache.parent.is_dir()  # created for the token cache
    assert made["auth"].calls == [False]  # a fresh login, not a cached token
    out = capsys.readouterr()
    assert "Logged in to Spotify" in out.out
    assert TOKEN not in out.out + out.err + caplog.text


def test_login_without_a_client_id_is_a_clear_error(setup, monkeypatch, capsys):
    monkeypatch.setattr(login, "load_settings", lambda: Settings(anthropic_api_key="k"))
    assert login.main() == 2
    assert "SPOTIFY_CLIENT_ID" in capsys.readouterr().err


def test_a_refused_login_is_a_clear_error(setup, monkeypatch, capsys):
    _, made = setup
    made["auth"] = FakeAuth(error=SpotifyOauthError("access_denied"))
    assert login.main() == 1
    err = capsys.readouterr().err
    assert "Spotify login failed" in err
    assert "access_denied" in err


def test_the_real_login_auth_opens_the_browser_on_the_loopback_redirect(tmp_path):
    auth = login.login_auth("cid", tmp_path / "t.json")
    assert auth.open_browser is True
    assert auth.redirect_uri == "http://127.0.0.1:8888/callback"
    assert auth.cache_handler.cache_path == str(tmp_path / "t.json")
