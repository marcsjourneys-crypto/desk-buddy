"""The YouTube client: a fake HTTP function stands in for the Data API (no real requests).
The planted key must never show up in a log line, an error, a repr or a tool result."""

import io
import json
import logging
import traceback
import urllib.error
from urllib.parse import parse_qs, urlsplit

import pytest

from desk_buddy.config import Settings, load_settings
from desk_buddy.tools import youtube
from desk_buddy.tools.youtube import (
    NETWORK,
    NOT_FOUND,
    NOT_SET_UP,
    QUOTA,
    Video,
    YouTubeClient,
    YouTubeError,
    is_video_id,
    redact,
    watch_url,
)

KEY = "AIzaPLANTED-youtube-key-0004"
VIDEO_ID = "dQw4w9WgXcQ"


class FakeHttp:
    """Answers by endpoint: a dict (sent as JSON), bytes, or an exception to raise."""

    def __init__(self, **answers):
        self.answers = answers
        self.calls = []

    def __call__(self, url, timeout):
        parts = urlsplit(url)
        endpoint = parts.path.rsplit("/", 1)[-1]
        self.calls.append((endpoint, parse_qs(parts.query), timeout))
        answer = self.answers[endpoint]
        if callable(answer) and not isinstance(answer, dict):
            answer = answer(url)
        if isinstance(answer, BaseException):
            raise answer
        return answer if isinstance(answer, bytes) else json.dumps(answer).encode()


def http_error(url, status, reasons=(), details=()):
    body = {
        "error": {
            "code": status,
            "message": "API key not valid.",
            "errors": [{"reason": r} for r in reasons],
            "details": [{"reason": r} for r in details],
        }
    }
    return urllib.error.HTTPError(
        url, status, "Forbidden", {}, io.BytesIO(json.dumps(body).encode())
    )


def video_search(*items):
    return {"items": list(items)}


def video_item(video_id=VIDEO_ID, title="Lo-fi beats", channel="Chill Cow"):
    return {
        "id": {"kind": "youtube#video", "videoId": video_id},
        "snippet": {"title": title, "channelTitle": channel},
    }


def client(**answers):
    http = FakeHttp(**answers)
    return YouTubeClient(KEY, http=http), http


# --- search ---


def test_search_asks_for_five_safe_videos_with_the_key():
    yt, http = client(search=video_search(video_item()))
    assert yt.search_video("lo-fi beats") == Video(VIDEO_ID, "Lo-fi beats", "Chill Cow")
    ((endpoint, params, timeout),) = http.calls
    assert endpoint == "search" and timeout == youtube.TIMEOUT_S
    assert params == {
        "part": ["snippet"],
        "type": ["video"],
        "maxResults": ["5"],
        "q": ["lo-fi beats"],
        "safeSearch": ["moderate"],
        "key": [KEY],
    }


def test_search_skips_items_without_a_valid_video_id_and_decodes_entities():
    yt, _ = client(
        search=video_search(
            {"id": {"kind": "youtube#channel", "channelId": "UCabc"}},
            video_item(video_id="bad id!"),
            video_item(video_id="x" * 12),
            video_item(title="Rock &amp; roll &#39;n&#39;   more", channel=" A\nB "),
        )
    )
    assert yt.search_video("rock") == Video(VIDEO_ID, "Rock & roll 'n' more", "A B")


def test_search_with_nothing_found_is_none():
    assert client(search={"items": []})[0].search_video("zzz") is None
    assert client(search={})[0].search_video("zzz") is None


# --- latest from a channel ---


def channel_answers(**overrides):
    answers = {
        "search": {
            "items": [{"id": {"channelId": "UC123"}, "snippet": {"channelTitle": "Veritas"}}]
        },
        "channels": {"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UU123"}}}]},
        "playlistItems": {
            "items": [
                {
                    "snippet": {
                        "title": "New video",
                        "videoOwnerChannelTitle": "Veritasium",
                        "resourceId": {"kind": "youtube#video", "videoId": VIDEO_ID},
                    }
                }
            ]
        },
    }
    answers.update(overrides)
    return answers


def test_latest_from_channel_follows_channel_then_uploads_then_newest_item():
    yt, http = client(**channel_answers())
    assert yt.latest_from_channel("veritasium") == Video(VIDEO_ID, "New video", "Veritasium")
    assert [c[0] for c in http.calls] == ["search", "channels", "playlistItems"]
    search, channels, items = (c[1] for c in http.calls)
    assert search["type"] == ["channel"] and search["maxResults"] == ["1"]
    assert search["q"] == ["veritasium"]
    assert channels["part"] == ["contentDetails"] and channels["id"] == ["UC123"]
    assert items["playlistId"] == ["UU123"] and items["maxResults"] == ["1"]
    assert all(c[1]["key"] == [KEY] for c in http.calls)


def test_latest_from_channel_falls_back_to_the_channel_name():
    answers = channel_answers()
    del answers["playlistItems"]["items"][0]["snippet"]["videoOwnerChannelTitle"]
    yt, _ = client(**answers)
    assert yt.latest_from_channel("v").channel == "Veritas"


@pytest.mark.parametrize(
    "overrides",
    [
        {"search": {"items": []}},
        {"search": {"items": [{"id": {"channelId": "../x?y"}}]}},
        {"channels": {"items": []}},
        {"channels": {"items": [{"contentDetails": {}}]}},
        {"playlistItems": {"items": []}},
        {"playlistItems": {"items": [{"snippet": {"resourceId": {"videoId": "short"}}}]}},
    ],
)
def test_latest_from_channel_with_nothing_found_is_none(overrides):
    yt, _ = client(**channel_answers(**overrides))
    assert yt.latest_from_channel("nobody") is None


# --- errors ---


@pytest.mark.parametrize(
    ("status", "reasons", "details", "message"),
    [
        (403, ["quotaExceeded"], [], QUOTA),
        (403, ["dailyLimitExceeded"], [], QUOTA),
        (400, ["keyInvalid"], ["API_KEY_INVALID"], NOT_SET_UP),
        (403, ["accessNotConfigured"], [], NOT_SET_UP),
        (403, ["forbidden"], [], NOT_SET_UP),
        (400, [], [], NOT_SET_UP),
        (429, [], [], youtube.BUSY),
        (404, ["notFound"], [], NOT_FOUND),
        (500, [], [], NETWORK),
        (503, [], [], NETWORK),
    ],
)
def test_http_errors_become_sentences(status, reasons, details, message):
    yt, _ = client(search=lambda url: http_error(url, status, reasons, details))
    with pytest.raises(YouTubeError) as caught:
        yt.search_video("x")
    assert str(caught.value) == message


@pytest.mark.parametrize(
    "error",
    [
        urllib.error.URLError("getaddrinfo failed"),
        TimeoutError("timed out"),
        ConnectionResetError("reset"),
    ],
)
def test_network_failures_say_youtube_cant_be_reached(error):
    yt, _ = client(search=error)
    with pytest.raises(YouTubeError, match="can't reach YouTube"):
        yt.search_video("x")


def test_a_body_that_isnt_json_is_a_network_problem():
    yt, _ = client(search=b"<html>oops</html>")
    with pytest.raises(YouTubeError) as caught:
        yt.search_video("x")
    assert str(caught.value) == NETWORK
    yt, _ = client(search=b"[1, 2]")
    with pytest.raises(YouTubeError):
        yt.search_video("x")


def test_an_error_body_that_isnt_json_still_maps_by_status():
    def broken(url):
        return urllib.error.HTTPError(url, 403, "Forbidden", {}, io.BytesIO(b"nope"))

    yt, _ = client(search=broken)
    with pytest.raises(YouTubeError) as caught:
        yt.search_video("x")
    assert str(caught.value) == NOT_SET_UP


# --- the key never leaks ---


def everything_said(caught, caplog):
    """The error's text, repr and full printed traceback, plus every log line."""
    printed = "".join(traceback.format_exception(caught.value))
    logs = "\n".join(r.getMessage() for r in caplog.records)
    return "\n".join([str(caught.value), repr(caught.value), printed, logs])


@pytest.mark.parametrize(
    "answer",
    [
        lambda url: http_error(url, 403, ["quotaExceeded"]),
        lambda url: http_error(url, 400, ["keyInvalid"]),
        lambda url: urllib.error.URLError(f"failed for {url}"),
        lambda url: OSError(f"socket trouble at {url}"),
        lambda url: b"not json " + url.encode(),
    ],
)
def test_the_key_never_appears_in_errors_or_logs(answer, caplog):
    caplog.set_level(logging.DEBUG)
    yt, _ = client(search=answer)
    with pytest.raises(YouTubeError) as caught:
        yt.search_video("x")
    said = everything_said(caught, caplog)
    assert KEY not in said
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


def test_the_key_never_appears_in_logs_on_success(caplog):
    caplog.set_level(logging.DEBUG)
    yt, _ = client(**channel_answers())
    yt.latest_from_channel("v")
    assert caplog.records  # the redacted requests are logged at debug
    assert all(KEY not in r.getMessage() for r in caplog.records)
    assert any("key=REDACTED" in r.getMessage() for r in caplog.records)


def test_the_key_isnt_in_the_reprs():
    yt, _ = client()
    assert KEY not in repr(yt) and KEY not in str(yt)
    settings = Settings(anthropic_api_key="k", youtube_api_key=KEY)
    assert KEY not in repr(settings)


def test_redact_hides_only_the_key_value():
    url = f"https://www.googleapis.com/youtube/v3/search?q=a&key={KEY}&part=snippet"
    assert redact(url) == (
        "https://www.googleapis.com/youtube/v3/search?q=a&key=REDACTED&part=snippet"
    )
    assert redact(f"https://x/y?key={KEY}") == "https://x/y?key=REDACTED"


def test_a_client_needs_a_key():
    with pytest.raises(ValueError):
        YouTubeClient("")


# --- video IDs ---


@pytest.mark.parametrize("good", [VIDEO_ID, "a-b_c-D_e1Z", "___________"])
def test_valid_video_ids_build_the_watch_url(good):
    assert is_video_id(good)
    assert watch_url(good) == "https://www.youtube.com/watch?v=" + good


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "short",
        VIDEO_ID + "x",
        VIDEO_ID + "\n",
        "dQw4w9WgXc!",
        "dQw4w9WgXc&",
        "ëQw4w9WgXcQ",
        None,
        5,
    ],
)
def test_invalid_video_ids_are_refused(bad):
    assert not is_video_id(bad)
    with pytest.raises(ValueError):
        watch_url(bad)


# --- config ---


def test_the_key_comes_from_env_and_is_never_a_dashboard_setting(tmp_path):
    from desk_buddy.config import DASHBOARD_KEYS, ENV_KEYS

    assert "YOUTUBE_API_KEY" in ENV_KEYS and "YOUTUBE_API_KEY" not in DASHBOARD_KEYS
    env = tmp_path / ".env"
    env.write_text(f"ANTHROPIC_API_KEY=k\nYOUTUBE_API_KEY={KEY}\n", encoding="utf-8")
    assert load_settings(env, environ={}).youtube_api_key == KEY
    # The dashboard can't set it.
    settings = load_settings(
        tmp_path / "none.env",
        environ={"ANTHROPIC_API_KEY": "k"},
        dashboard={"YOUTUBE_API_KEY": KEY},
    )
    assert settings.youtube_api_key == ""
