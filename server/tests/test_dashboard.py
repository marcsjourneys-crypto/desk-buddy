"""The dashboard server on a real loopback socket: no secret ever leaves it, changes need
the page's token and our Origin, the WebSocket needs our Origin, foreign Host headers
(DNS rebinding) are refused, it binds 127.0.0.1, and a busy port isn't fatal."""

import asyncio
import contextlib
import json
import logging
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass

import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from desk_buddy.apps_store import AppsStore
from desk_buddy.config import Settings
from desk_buddy.dashboard import build_status, create_dashboard, start_dashboard
from desk_buddy.events import EventBus
from desk_buddy.personality_store import PersonalityStore
from desk_buddy.settings_store import LiveSettings, SettingsStore

SECRETS = ("sk-ant-PLANTED-0001", "robot-token-PLANTED-0002", "spotify-id-PLANTED-0003")
TOKEN = "csrf-token-for-tests"
WINDOWS_APPS = [
    {"Name": "Notepad", "AppID": "Microsoft.WindowsNotepad_8wekyb3d8bbwe!App"},
    {"Name": "<b>Edge</b>", "AppID": "MSEdge"},
]


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class Dash:
    port: int
    live: LiveSettings
    bus: EventBus
    settings: SettingsStore
    personality: PersonalityStore
    apps: AppsStore
    listings: list

    @property
    def base(self):
        return f"http://127.0.0.1:{self.port}"

    @property
    def origin(self):
        return f"http://127.0.0.1:{self.port}"


@contextlib.asynccontextmanager
async def running(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        f"ANTHROPIC_API_KEY={SECRETS[0]}\nROBOT_TOKEN={SECRETS[1]}\n"
        f"SPOTIFY_CLIENT_ID={SECRETS[2]}\nTTS_ENGINE=sapi\n",
        encoding="utf-8",
    )
    settings = SettingsStore(tmp_path / "settings.json", env_path=env, environ={})
    personality = PersonalityStore(tmp_path / "personality.json")
    live = LiveSettings(settings.settings())
    bus = EventBus()
    listings = []

    def runner(args, timeout):  # never PowerShell
        listings.append(tuple(args))
        return json.dumps(WINDOWS_APPS).encode()

    apps = AppsStore(tmp_path / "apps.json", tmp_path / "bookmarks.json", runner=runner)
    port = free_port()
    app = create_dashboard(
        port=port,
        token=TOKEN,
        settings=settings,
        live=live,
        personality=personality,
        bus=bus,
        status=lambda: build_status(
            live.current, voice="sapi", tools=(), robot=None, state="idle", dashboard_port=port
        ),
        preview=lambda: "THE PROMPT " + personality.current().character,
        apps=apps,
    )
    server = await start_dashboard(app, port)
    assert server is not None
    try:
        yield Dash(port, live, bus, settings, personality, apps, listings)
    finally:
        await asyncio.wait_for(server.stop(), 5)


def request(method, url, *, body=None, headers=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, dict(resp.headers), resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read().decode()


async def call(method, url, **kwargs):
    return await asyncio.to_thread(request, method, url, **kwargs)


def change_headers(d, **extra):
    return {"Origin": d.origin, "X-CARL-Token": TOKEN, **extra}


# --- the page and the API ---


def test_the_page_carries_the_token_and_a_strict_policy(tmp_path):
    async def run():
        async with running(tmp_path) as d:
            status, headers, body = await call("GET", d.base + "/")
            assert status == 200
            assert f'content="{TOKEN}"' in body
            assert "__CARL_TOKEN__" not in body
            csp = headers["content-security-policy"]
            assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp
            assert headers["x-content-type-options"] == "nosniff"
            assert headers["cache-control"] == "no-store"
            for name in ("app.js", "style.css"):
                status, _, _ = await call("GET", f"{d.base}/static/{name}")
                assert status == 200
            status, _, _ = await call("GET", d.base + "/static/../dashboard.py")
            assert status == 404

    asyncio.run(run())


def test_no_secret_ever_appears_in_any_response(tmp_path):
    async def run():
        async with running(tmp_path) as d:
            bodies = []
            for path in ("/", "/api/status", "/api/settings", "/api/personality"):
                bodies.append((await call("GET", d.base + path))[2])
            bodies.append(
                (
                    await call(
                        "PUT",
                        d.base + "/api/settings",
                        body={"MAX_SENTENCES": 4},
                        headers=change_headers(d),
                    )
                )[2]
            )
            bodies.append(
                (
                    await call(
                        "PUT",
                        d.base + "/api/settings",
                        body={"ANTHROPIC_API_KEY": "x", "MAX_SENTENCES": "zero"},
                        headers=change_headers(d),
                    )
                )[2]
            )
            d.bus.publish("state", name="idle")
            async with connect(f"ws://127.0.0.1:{d.port}/api/events", origin=d.origin) as ws:
                bodies.append(await asyncio.wait_for(ws.recv(), 2))
            return "\n".join(bodies)

    text = asyncio.run(run())
    for secret in SECRETS:
        assert secret not in text


def test_status_has_no_secrets_only_whether_things_are_on():
    s = Settings(
        anthropic_api_key=SECRETS[0],
        robot_token=SECRETS[1],
        spotify_client_id=SECRETS[2],
        computer_control=True,
    )
    status = build_status(
        s, voice="piper x", tools=("play_music",), robot=None, state="idle", dashboard_port=8766
    )
    assert status["robot"] == {"enabled": True, "connected": False, "fw_version": None}
    assert status["music"] is True and status["notes"] is False
    assert status["dashboard"] == "http://127.0.0.1:8766"
    for secret in SECRETS:
        assert secret not in json.dumps(status)
    # Computer control turned off a moment ago: the status says so, music too.
    off = build_status(
        s,
        voice="piper x",
        tools=("play_music",),
        robot=None,
        state="idle",
        dashboard_port=8766,
        computer_control=False,
    )
    assert (off["computer_control"], off["music"]) == (False, False)


def test_settings_change_and_reset_with_the_token(tmp_path):
    async def run():
        async with running(tmp_path) as d:
            status, _, body = await call(
                "PUT",
                d.base + "/api/settings",
                body={"MAX_SENTENCES": 4, "COMPUTER_CONTROL": True},
                headers=change_headers(d),
            )
            assert status == 200, body
            view = {e["key"]: e for e in json.loads(body)["settings"]}
            assert view["MAX_SENTENCES"]["value"] == 4
            assert view["MAX_SENTENCES"]["source"] == "dashboard"
            d.live.apply()
            assert d.live.current.max_sentences == 4  # handed to CARL for the next question
            status, _, body = await call(
                "PUT",
                d.base + "/api/settings",
                body={"MAX_SENTENCES": 0},
                headers=change_headers(d),
            )
            assert status == 400 and "MAX_SENTENCES" in json.loads(body)["errors"]
            status, _, body = await call(
                "DELETE", d.base + "/api/settings/MAX_SENTENCES", headers=change_headers(d)
            )
            assert status == 200
            d.live.apply()
            assert d.live.current.max_sentences == 2
            status, _, _ = await call(
                "DELETE", d.base + "/api/settings/ROBOT_TOKEN", headers=change_headers(d)
            )
            assert status == 404

    asyncio.run(run())


def test_personality_change_preview_and_reset(tmp_path):
    async def run():
        async with running(tmp_path) as d:
            status, _, body = await call(
                "PUT",
                d.base + "/api/personality",
                body={"character": "You are grumpy.", "lines": {"greeting": {"text": "Hmph."}}},
                headers=change_headers(d),
            )
            assert status == 200, body
            view = json.loads(body)
            assert view["preview"] == "THE PROMPT You are grumpy."
            assert view["lines"]["greeting"]["text"] == "Hmph."
            status, _, body = await call(
                "PUT",
                d.base + "/api/personality",
                body={"character": "You are {bad}."},
                headers=change_headers(d),
            )
            assert status == 400 and "character" in json.loads(body)["errors"]
            status, _, body = await call(
                "DELETE", d.base + "/api/personality/character", headers=change_headers(d)
            )
            assert status == 200
            assert d.personality.current().character != "You are grumpy."
            status, _, _ = await call(
                "DELETE", d.base + "/api/personality/system", headers=change_headers(d)
            )
            assert status == 404

    asyncio.run(run())


def test_about_you_change_and_reset_through_the_api(tmp_path):
    me = {"name": "Marta", "city": "Leeds", "region": "", "country": "gb", "notes": "a\nb"}

    async def run():
        async with running(tmp_path) as d:
            status, _, body = await call("GET", d.base + "/api/personality")
            assert status == 200
            view = json.loads(body)
            assert view["about"] == dict.fromkeys(me, "")
            assert view["limits"]["about"]["country"] == 2
            status, _, body = await call(
                "PUT", d.base + "/api/personality", body={"about": me}, headers=change_headers(d)
            )
            assert status == 200, body
            assert json.loads(body)["about"] == {**me, "country": "GB"}
            assert d.personality.current().about.location == {"city": "Leeds", "country": "GB"}
            status, _, body = await call(
                "PUT",
                d.base + "/api/personality",
                body={"about": {"name": "two\nlines", "country": "Britain"}},
                headers=change_headers(d),
            )
            assert status == 400
            assert set(json.loads(body)["errors"]) == {"about.name", "about.country"}
            assert d.personality.current().about.name == "Marta"  # all or nothing
            status, _, body = await call(
                "DELETE", d.base + "/api/personality/about.city", headers=change_headers(d)
            )
            assert status == 200, body
            assert json.loads(body)["about"]["city"] == ""
            assert d.personality.current().about.name == "Marta"
            status, _, _ = await call(
                "DELETE", d.base + "/api/personality/about.shoe", headers=change_headers(d)
            )
            assert status == 404
            # Without the token, nothing changes.
            status, _, _ = await call(
                "PUT",
                d.base + "/api/personality",
                body={"about": {"name": "Mallory"}},
                headers={"Origin": d.origin},
            )
            assert status == 403
            assert d.personality.current().about.name == "Marta"

    asyncio.run(run())


def test_no_secret_appears_in_about_you_responses(tmp_path):
    async def run():
        async with running(tmp_path) as d:
            bodies = []
            for body in (
                {"about": {"name": "Marta", "notes": "Hi."}},
                {"about": {"name": "x" * 500, "country": "nope"}},
                {"about": {"ANTHROPIC_API_KEY": "x"}},
            ):
                bodies.append(
                    (
                        await call(
                            "PUT", d.base + "/api/personality", body=body, headers=change_headers(d)
                        )
                    )[2]
                )
            bodies.append(
                (
                    await call(
                        "DELETE", d.base + "/api/personality/about.name", headers=change_headers(d)
                    )
                )[2]
            )
            bodies.append((await call("GET", d.base + "/api/personality"))[2])
            return "\n".join(bodies)

    text = asyncio.run(run())
    assert "about" in text
    for secret in SECRETS:
        assert secret not in text


def test_a_failed_save_is_a_json_error_never_a_500(tmp_path, monkeypatch):
    import desk_buddy.settings_store as settings_store

    def disk_full(path, data):
        raise OSError("disk full")

    monkeypatch.setattr(settings_store, "write_json_atomic", disk_full)
    import desk_buddy.personality_store as personality_store

    monkeypatch.setattr(personality_store, "write_json_atomic", disk_full)

    async def run():
        async with running(tmp_path) as d:
            results = [
                await call(
                    "PUT",
                    d.base + "/api/settings",
                    body={"MAX_SENTENCES": 4},
                    headers=change_headers(d),
                ),
                await call(
                    "DELETE", d.base + "/api/settings/MAX_SENTENCES", headers=change_headers(d)
                ),
                await call(
                    "PUT",
                    d.base + "/api/personality",
                    body={"character": "Hi."},
                    headers=change_headers(d),
                ),
                await call(
                    "DELETE", d.base + "/api/personality/character", headers=change_headers(d)
                ),
            ]
            return results

    for status, _, body in asyncio.run(run()):
        assert status == 503
        assert "Couldn't save" in json.loads(body)["error"]


@pytest.mark.parametrize("length", ["abc", "-5", str(10**9)])
def test_odd_or_huge_content_lengths_are_refused_cleanly(tmp_path, length):
    import http.client

    async def run():
        async with running(tmp_path) as d:

            def send():
                conn = http.client.HTTPConnection("127.0.0.1", d.port, timeout=5)
                conn.putrequest("PUT", "/api/settings", skip_accept_encoding=True)
                conn.putheader("Origin", d.origin)
                conn.putheader("X-CARL-Token", TOKEN)
                conn.putheader("Content-Length", length)
                conn.endheaders()
                try:
                    return conn.getresponse().status
                finally:
                    conn.close()

            return await asyncio.to_thread(send)

    assert asyncio.run(run()) in (400, 413)


def test_a_body_bigger_than_the_cap_is_refused_even_without_a_length(tmp_path):
    import http.client

    async def run():
        async with running(tmp_path) as d:

            def send():
                conn = http.client.HTTPConnection("127.0.0.1", d.port, timeout=5)
                big = b'{"x": "' + b"a" * (200 * 1024) + b'"}'
                conn.request(
                    "PUT",
                    "/api/settings",
                    body=iter([big]),  # chunked: no Content-Length
                    headers={"Origin": d.origin, "X-CARL-Token": TOKEN},
                    encode_chunked=True,
                )
                try:
                    return conn.getresponse().status
                finally:
                    conn.close()

            return await asyncio.to_thread(send)

    assert asyncio.run(run()) == 413


# --- apps and bookmarks ---


def test_find_apps_then_save_the_allowlist(tmp_path):
    async def run():
        async with running(tmp_path) as d:
            put = {"apps": [{"app_id": "MSEdge", "names": ["Browser", "edge"]}]}
            # Not listed yet: refused.
            status, _, body = await call(
                "PUT", d.base + "/api/apps", body=put, headers=change_headers(d)
            )
            assert status == 400 and "apps.0" in json.loads(body)["errors"]
            status, _, body = await call(
                "GET", d.base + "/api/apps/available", headers={"X-CARL-Token": TOKEN}
            )
            assert status == 200, body
            assert json.loads(body)["apps"] == [
                {"name": "<b>Edge</b>", "app_id": "MSEdge"},  # data, as is
                {"name": "Notepad", "app_id": "Microsoft.WindowsNotepad_8wekyb3d8bbwe!App"},
            ]
            assert len(d.listings) == 1
            status, _, body = await call(
                "PUT", d.base + "/api/apps", body=put, headers=change_headers(d)
            )
            assert status == 200, body
            view = json.loads(body)
            assert view["apps"] == [
                {"app_id": "MSEdge", "title": "<b>Edge</b>", "names": ["browser", "edge"]}
            ]
            assert view["limits"] == {"apps": 50, "names": 3, "name": 40}
            status, _, body = await call("GET", d.base + "/api/apps")
            assert json.loads(body)["apps"] == view["apps"]
            status, _, body = await call(
                "PUT", d.base + "/api/apps", body={"apps": "x"}, headers=change_headers(d)
            )
            assert status == 400

    asyncio.run(run())


def test_bookmarks_round_trip_through_the_api(tmp_path):
    async def run():
        async with running(tmp_path) as d:
            status, _, body = await call("GET", d.base + "/api/bookmarks")
            assert status == 200 and json.loads(body)["bookmarks"] == []
            good = {"bookmarks": [{"name": "Weather", "url": "https://example.com/weather"}]}
            status, _, body = await call(
                "PUT", d.base + "/api/bookmarks", body=good, headers=change_headers(d)
            )
            assert status == 200, body
            assert json.loads(body)["bookmarks"] == [
                {"name": "weather", "url": "https://example.com/weather"}
            ]
            bad = {"bookmarks": [{"name": "evil", "url": "file:///C:/Windows/notepad.exe"}]}
            status, _, body = await call(
                "PUT", d.base + "/api/bookmarks", body=bad, headers=change_headers(d)
            )
            assert status == 400 and "bookmarks.0" in json.loads(body)["errors"]
            assert d.apps.snapshot()[2][0].name == "weather"  # unchanged

    asyncio.run(run())


@pytest.mark.parametrize(
    "headers",
    [{}, {"X-CARL-Token": "wrong"}, {"Origin": "ORIGIN"}],
    ids=["none", "wrong", "origin-only"],
)
def test_listing_the_apps_needs_the_token(tmp_path, headers):
    async def run():
        async with running(tmp_path) as d:
            sent = {k: (d.origin if v == "ORIGIN" else v) for k, v in headers.items()}
            status, _, _ = await call("GET", d.base + "/api/apps/available", headers=sent)
            assert status == 403
            assert d.listings == []  # PowerShell never started

    asyncio.run(run())


def test_a_failed_listing_is_a_json_error(tmp_path):
    from desk_buddy.apps_store import ListError

    async def run():
        async with running(tmp_path) as d:

            def broken(args, timeout):
                raise ListError("Windows took too long to list the apps.")

            d.apps._runner = broken
            status, _, body = await call(
                "GET", d.base + "/api/apps/available", headers={"X-CARL-Token": TOKEN}
            )
            assert status == 503
            assert json.loads(body)["error"] == "Windows took too long to list the apps."

    asyncio.run(run())


def test_a_corrupt_apps_file_shows_as_a_banner(tmp_path):
    (tmp_path / "apps.json").write_text("{broken", encoding="utf-8")

    async def run():
        async with running(tmp_path) as d:
            return [json.loads((await call("GET", d.base + p))[2]) for p in ("/api/apps",)]

    (view,) = asyncio.run(run())
    assert "apps.json" in view["error"] and view["apps"] == []


def test_no_secret_appears_in_apps_responses(tmp_path):
    async def run():
        async with running(tmp_path) as d:
            bodies = [
                (
                    await call(
                        "GET", d.base + "/api/apps/available", headers={"X-CARL-Token": TOKEN}
                    )
                )[2]
            ]
            for path, body in (
                ("/api/apps", {"apps": [{"app_id": "MSEdge", "names": ["browser"]}]}),
                ("/api/apps", {"apps": [{"app_id": "ANTHROPIC_API_KEY", "names": ["k"]}]}),
                ("/api/bookmarks", {"bookmarks": [{"name": "a", "url": "https://a.example"}]}),
                ("/api/bookmarks", {"bookmarks": [{"name": "a", "url": "nope"}]}),
            ):
                bodies.append(
                    (await call("PUT", d.base + path, body=body, headers=change_headers(d)))[2]
                )
            for path in ("/api/apps", "/api/bookmarks"):
                bodies.append((await call("GET", d.base + path))[2])
            return "\n".join(bodies)

    text = asyncio.run(run())
    assert "browser" in text
    for secret in SECRETS:
        assert secret not in text


def test_a_failed_apps_save_is_a_json_error(tmp_path, monkeypatch):
    import desk_buddy.apps_store as apps_store

    def disk_full(path, data):
        raise OSError("disk full")

    monkeypatch.setattr(apps_store, "write_json_atomic", disk_full)

    async def run():
        async with running(tmp_path) as d:
            return [
                await call(
                    "PUT", d.base + "/api/apps", body={"apps": []}, headers=change_headers(d)
                ),
                await call(
                    "PUT",
                    d.base + "/api/bookmarks",
                    body={"bookmarks": []},
                    headers=change_headers(d),
                ),
            ]

    for status, _, body in asyncio.run(run()):
        assert status == 503
        assert "Couldn't save" in json.loads(body)["error"]


# --- CSRF and rebinding ---


@pytest.mark.parametrize(
    "headers",
    [
        {},  # neither
        {"X-CARL-Token": TOKEN},  # no Origin
        {"Origin": "ORIGIN"},  # no token
        {"Origin": "ORIGIN", "X-CARL-Token": "wrong"},
        {"Origin": "http://evil.example", "X-CARL-Token": TOKEN},
        {"Origin": "http://127.0.0.1:1", "X-CARL-Token": TOKEN},
        {"Origin": "null", "X-CARL-Token": TOKEN},
    ],
)
def test_changes_without_the_token_and_our_origin_are_refused(tmp_path, headers):
    async def run():
        async with running(tmp_path) as d:
            sent = {k: (d.origin if v == "ORIGIN" else v) for k, v in headers.items()}
            d.apps.available()  # so a refused app change isn't refused for its ID
            results = [
                await call(
                    "PUT", d.base + "/api/settings", body={"MAX_SENTENCES": 4}, headers=sent
                ),
                await call(
                    "PUT", d.base + "/api/personality", body={"character": "x"}, headers=sent
                ),
                await call("DELETE", d.base + "/api/settings/MAX_SENTENCES", headers=sent),
                await call("DELETE", d.base + "/api/personality/character", headers=sent),
                await call(
                    "PUT",
                    d.base + "/api/apps",
                    body={"apps": [{"app_id": "MSEdge", "names": ["browser"]}]},
                    headers=sent,
                ),
                await call(
                    "PUT",
                    d.base + "/api/bookmarks",
                    body={"bookmarks": [{"name": "x", "url": "https://x.example"}]},
                    headers=sent,
                ),
            ]
            assert [r[0] for r in results] == [403] * 6
            assert d.settings.values == {}
            assert not (tmp_path / "personality.json").exists()
            assert not (tmp_path / "apps.json").exists()
            assert not (tmp_path / "bookmarks.json").exists()

    asyncio.run(run())


def test_localhost_origin_is_ours_too(tmp_path):
    async def run():
        async with running(tmp_path) as d:
            status, _, _ = await call(
                "PUT",
                d.base + "/api/settings",
                body={"MAX_SENTENCES": 4},
                headers=change_headers(d, Origin=f"http://localhost:{d.port}"),
            )
            assert status == 200

    asyncio.run(run())


def test_a_foreign_host_header_is_refused_dns_rebinding(tmp_path):
    async def run():
        async with running(tmp_path) as d:
            for path in ("/", "/api/settings", "/api/status", "/api/apps", "/api/bookmarks"):
                status, _, body = await call(
                    "GET", d.base + path, headers={"Host": f"evil.example:{d.port}"}
                )
                assert status == 403
                assert SECRETS[0] not in body
            status, _, _ = await call(
                "GET",
                d.base + "/api/apps/available",
                headers={"Host": f"evil.example:{d.port}", "X-CARL-Token": TOKEN},
            )
            assert status == 403 and d.listings == []

    asyncio.run(run())


def test_the_event_socket_needs_our_origin(tmp_path):
    async def run():
        async with running(tmp_path) as d:
            for origin in ("http://evil.example", None):
                with pytest.raises(InvalidStatus):
                    async with connect(f"ws://127.0.0.1:{d.port}/api/events", origin=origin):
                        pass

    asyncio.run(run())


def test_the_event_socket_sends_history_then_live_events(tmp_path):
    async def run():
        async with running(tmp_path) as d:
            d.bus.publish("state", name="listening")
            async with connect(f"ws://127.0.0.1:{d.port}/api/events", origin=d.origin) as ws:
                first = json.loads(await asyncio.wait_for(ws.recv(), 2))
                d.bus.publish("heard", text="<b>hello</b>", stt_ms=300)
                second = json.loads(await asyncio.wait_for(ws.recv(), 2))
            return first, second

    first, second = asyncio.run(run())
    assert first["type"] == "state" and first["name"] == "listening"
    assert second["type"] == "heard" and second["text"] == "<b>hello</b>"  # data, as is


# --- where and whether it listens ---


def test_it_binds_localhost_only(tmp_path):
    async def run():
        port = free_port()
        app = create_dashboard(
            port=port,
            token=TOKEN,
            settings=SettingsStore(
                tmp_path / "s.json", env_path=None, environ={"ANTHROPIC_API_KEY": "k"}
            ),
            live=LiveSettings(Settings(anthropic_api_key="k")),
            personality=PersonalityStore(tmp_path / "p.json"),
            bus=EventBus(),
            status=dict,
            preview=str,
        )
        server = await start_dashboard(app, port)
        try:
            return server.host
        finally:
            await server.stop()

    assert asyncio.run(run()) == "127.0.0.1"


def test_a_busy_port_is_not_fatal(tmp_path, caplog):
    caplog.set_level(logging.INFO)

    async def run():
        with socket.socket() as busy:
            busy.bind(("127.0.0.1", 0))
            busy.listen()
            port = busy.getsockname()[1]
            app = create_dashboard(
                port=port,
                token=TOKEN,
                settings=SettingsStore(
                    tmp_path / "s.json", env_path=None, environ={"ANTHROPIC_API_KEY": "k"}
                ),
                live=LiveSettings(Settings(anthropic_api_key="k")),
                personality=PersonalityStore(tmp_path / "p.json"),
                bus=EventBus(),
                status=dict,
                preview=str,
            )
            return await start_dashboard(app, port)

    assert asyncio.run(run()) is None
    assert "Dashboard: off" in caplog.text
