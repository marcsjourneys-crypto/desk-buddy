"""SpotifyControl with a fake spotipy client: device choice, start-and-wait, error mapping,
and the server-side login guard. No network."""

import logging

import pytest
import requests
from spotipy.exceptions import SpotifyException, SpotifyOauthError

from desk_buddy.tools import spotify
from desk_buddy.tools.registry import ToolError
from desk_buddy.tools.spotify import MusicError, SpotifyControl

PC = "MARC-LAPTOP"
MINE = {"id": "pc", "name": "marc-laptop", "type": "Computer", "is_active": False}
PHONE = {"id": "phone", "name": "Pixel", "type": "Smartphone", "is_active": True}
OTHER_PC = {"id": "other", "name": "STUDIO", "type": "Computer", "is_active": False}
PHONE_IDLE = {**PHONE, "is_active": False}


class FakeSpotify:
    """Returns the next device list on each devices() call (the last one repeats)."""

    def __init__(self, *device_lists, error=None, transfer_404s=0):
        self.device_lists = list(device_lists) or [[]]
        self.error = error
        self.transfer_404s = transfer_404s  # the first N transfers fail: device not ready
        self.calls = []

    def devices(self):
        self.calls.append(("devices",))
        if self.error:
            raise self.error
        lists = self.device_lists
        return {"devices": lists.pop(0) if len(lists) > 1 else lists[0]}

    def transfer_playback(self, device_id, force_play=True):
        self.calls.append(("transfer_playback", device_id, force_play))
        if self.transfer_404s:
            self.transfer_404s -= 1
            raise SpotifyException(404, -1, "Device not found")


class FakeTime:
    def __init__(self):
        self.now = 0.0

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def make(client, *, started=None):
    t = FakeTime()
    starts = [] if started is None else started
    control = SpotifyControl(
        client, pc_name=PC, clock=t.clock, sleep=t.sleep, start_app=lambda: starts.append(1)
    )
    return control, t, starts


# --- device choice ---


def test_this_pcs_device_comes_first_and_is_made_active():
    client = FakeSpotify([PHONE, OTHER_PC, MINE])
    control, _, starts = make(client)
    assert control.ensure_device() == "pc"
    assert ("transfer_playback", "pc", False) in client.calls  # music doesn't start by itself
    assert starts == []


def test_an_already_active_device_is_not_transferred():
    client = FakeSpotify([{**MINE, "is_active": True}])
    control, _, _ = make(client)
    assert control.ensure_device() == "pc"
    assert not [c for c in client.calls if c[0] == "transfer_playback"]


def test_the_active_device_is_used_if_this_pc_has_none():
    client = FakeSpotify([OTHER_PC, PHONE])
    control, _, starts = make(client)
    assert control.ensure_device() == "phone"
    assert starts == []


def test_an_idle_computer_with_another_name_is_used_without_starting_spotify():
    # The PC's Spotify device name can differ from the hostname (it's set in Spotify).
    client = FakeSpotify([PHONE_IDLE, OTHER_PC])
    control, _, starts = make(client)
    assert control.ensure_device() == "other"
    assert starts == []


@pytest.mark.parametrize(
    "unusable",
    [
        {**MINE, "id": None},
        {**MINE, "id": ""},
        {**MINE, "is_restricted": True},
        {**PHONE, "is_restricted": True},
    ],
)
def test_devices_without_an_id_or_restricted_are_ignored(unusable):
    client = FakeSpotify([unusable], [unusable, {**MINE, "id": "fresh"}])
    control, _, starts = make(client)
    assert control.ensure_device() == "fresh"
    assert starts == [1]


def test_spotify_is_started_and_waited_for_when_no_device_is_usable():
    client = FakeSpotify([PHONE_IDLE], [PHONE_IDLE], [PHONE_IDLE, MINE])
    control, t, starts = make(client)
    assert control.ensure_device() == "pc"
    assert starts == [1]
    assert 0 < t.now <= spotify.START_WAIT_SECONDS
    assert ("transfer_playback", "pc", False) in client.calls


def test_after_starting_a_new_computer_device_is_accepted_even_if_named_differently():
    renamed = {"id": "new", "name": "Marc's PC", "type": "Computer", "is_active": False}
    client = FakeSpotify([], [renamed])
    control, _, _ = make(client)
    assert control.ensure_device() == "new"


def test_a_device_that_isnt_ready_yet_is_retried_twice():
    client = FakeSpotify([], [MINE], transfer_404s=2)
    control, _, _ = make(client)
    assert control.ensure_device() == "pc"
    assert [c[0] for c in client.calls].count("transfer_playback") == 3


def test_a_device_that_never_gets_ready_says_so():
    client = FakeSpotify([], [MINE], transfer_404s=3)
    control, _, _ = make(client)
    with pytest.raises(MusicError) as info:
        control.ensure_device()
    assert str(info.value) == spotify.NOT_READY


def test_spotify_that_never_appears_is_a_clear_error_after_8_seconds():
    client = FakeSpotify([])
    control, t, starts = make(client)
    with pytest.raises(MusicError, match="Spotify didn't open"):
        control.ensure_device()
    assert starts == [1]
    assert spotify.START_WAIT_SECONDS <= t.now <= spotify.START_WAIT_SECONDS + 1


def test_a_failure_to_launch_spotify_is_a_clear_error():
    def broken():
        raise OSError("no handler for spotify:")

    control = SpotifyControl(FakeSpotify([]), pc_name=PC, start_app=broken)
    with pytest.raises(MusicError, match="Spotify didn't open"):
        control.ensure_device()


def test_music_errors_are_tool_errors():
    assert issubclass(MusicError, ToolError)


# --- error mapping ---


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (SpotifyException(403, -1, "x", reason="PREMIUM_REQUIRED"), spotify.PREMIUM),
        (SpotifyException(401, -1, "The access token expired"), spotify.NOT_LOGGED_IN),
        (SpotifyOauthError("invalid_grant"), spotify.NOT_LOGGED_IN),
        (SpotifyException(404, -1, "x", reason="NO_ACTIVE_DEVICE"), spotify.NO_DEVICE),
        (SpotifyException(404, -1, "Not found"), spotify.NOT_FOUND),
        (SpotifyException(429, -1, "Max Retries"), spotify.BUSY),
        (SpotifyException(502, -1, "Bad gateway"), spotify.NETWORK),
        (SpotifyException(403, -1, "Restriction violated", reason="UNKNOWN"), spotify.REFUSED),
        (SpotifyException(400, -1, "Bad request"), spotify.REFUSED),
        (requests.exceptions.ConnectionError("offline"), spotify.NETWORK),
        (requests.exceptions.ReadTimeout("slow"), spotify.NETWORK),
    ],
)
def test_spotify_errors_become_spoken_messages(error, message):
    control, _, _ = make(FakeSpotify(error=error))
    with pytest.raises(MusicError) as info:
        control.ensure_device()
    assert str(info.value) == message


def test_an_unexpected_error_is_not_hidden():
    control, _, _ = make(FakeSpotify(error=KeyError("devices")))
    with pytest.raises(KeyError):
        control.ensure_device()


def test_a_call_that_hangs_gives_up_at_the_deadline(monkeypatch):
    import threading

    monkeypatch.setattr(spotify, "CALL_DEADLINE_S", 0.1)
    release = threading.Event()

    class Hanging(FakeSpotify):
        def devices(self):
            release.wait(5)
            return {"devices": []}

    control, _, _ = make(Hanging())
    try:
        with pytest.raises(MusicError) as info:
            control.call("devices")
        assert str(info.value) == spotify.NETWORK
    finally:
        release.set()


def test_the_api_client_never_sleeps_on_retry_after(tmp_path):
    # status_retries=0: a 429 comes back at once instead of urllib3 sleeping for an
    # unbounded Retry-After.
    cache = tmp_path / "spotify_token.json"
    cache.write_text('{"access_token": "a", "refresh_token": "r"}')
    client = spotify.create_control("client-id", cache_path=cache).client
    assert client.status_retries == 0
    assert client.requests_timeout <= spotify.CALL_DEADLINE_S


# --- the server's login: never interactive ---


def test_the_server_never_starts_an_interactive_login():
    auth = spotify.server_auth("client-id", cache_path="unused.json")
    with pytest.raises(MusicError, match="run the Spotify login"):
        auth.get_authorization_code()


def test_the_auth_settings(tmp_path):
    cache = tmp_path / "spotify_token.json"
    auth = spotify.server_auth("client-id", cache_path=cache)
    assert auth.client_id == "client-id"
    assert auth.redirect_uri == "http://127.0.0.1:8888/callback"
    assert set(auth.scope.split()) == set(spotify.SCOPES.split())
    assert auth.cache_handler.cache_path == str(cache)
    assert auth.requests_timeout == spotify.REQUEST_TIMEOUT_S


def test_token_cache_lives_in_app_data(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert spotify.token_cache_path() == tmp_path / "desk-buddy" / "spotify_token.json"


def test_logged_in_needs_a_saved_refresh_token(tmp_path):
    cache = tmp_path / "spotify_token.json"
    assert not spotify.logged_in(cache)
    cache.write_text("not json")
    assert not spotify.logged_in(cache)
    cache.write_text('{"access_token": "a", "refresh_token": "r", "expires_at": 1}')
    assert spotify.logged_in(cache)


def test_create_control_is_none_without_a_client_id_or_a_login(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    cache = tmp_path / "spotify_token.json"
    assert spotify.create_control("", cache_path=cache) is None
    assert spotify.create_control("client-id", cache_path=cache) is None
    cache.write_text('{"access_token": "SECRET-A", "refresh_token": "SECRET-R"}')
    control = spotify.create_control("client-id", cache_path=cache)
    assert isinstance(control, SpotifyControl)
    assert "SECRET" not in caplog.text


def test_spotipy_debug_logs_are_silenced():
    # spotipy logs request headers (the bearer token) and refresh bodies at DEBUG.
    assert logging.getLogger("spotipy").getEffectiveLevel() >= logging.WARNING
