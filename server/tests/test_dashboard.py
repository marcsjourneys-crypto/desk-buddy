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

from desk_buddy.config import Settings
from desk_buddy.dashboard import build_status, create_dashboard, start_dashboard
from desk_buddy.events import EventBus
from desk_buddy.personality_store import PersonalityStore
from desk_buddy.settings_store import LiveSettings, SettingsStore

SECRETS = ("sk-ant-PLANTED-0001", "robot-token-PLANTED-0002", "spotify-id-PLANTED-0003")
TOKEN = "csrf-token-for-tests"


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
    )
    server = await start_dashboard(app, port)
    assert server is not None
    try:
        yield Dash(port, live, bus, settings, personality)
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
            results = [
                await call(
                    "PUT", d.base + "/api/settings", body={"MAX_SENTENCES": 4}, headers=sent
                ),
                await call(
                    "PUT", d.base + "/api/personality", body={"character": "x"}, headers=sent
                ),
                await call("DELETE", d.base + "/api/settings/MAX_SENTENCES", headers=sent),
                await call("DELETE", d.base + "/api/personality/character", headers=sent),
            ]
            assert [r[0] for r in results] == [403, 403, 403, 403]
            assert d.settings.values == {}
            assert not (tmp_path / "personality.json").exists()

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
            for path in ("/", "/api/settings", "/api/status"):
                status, _, body = await call(
                    "GET", d.base + path, headers={"Host": f"evil.example:{d.port}"}
                )
                assert status == 403
                assert SECRETS[0] not in body

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
