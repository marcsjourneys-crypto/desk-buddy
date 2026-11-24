"""The robot WebSocket route, served by a real uvicorn on 127.0.0.1 with a free port."""

import asyncio
import contextlib
import logging

import pytest

from desk_buddy.fake_robot import FakeRobot
from desk_buddy.link.switch import DeviceSwitch
from desk_buddy.server import (
    CLOSE_BAD_TOKEN,
    CLOSE_NO_HELLO,
    CLOSE_REPLACED,
    ServerStartError,
    create_app,
    start_server,
)

TOKEN = "t" * 20


class NullMic:
    def start(self):
        pass

    def stop(self):
        return b""


class NullPlayer:
    def current_token(self):
        return 0

    def play(self, pcm, token):
        pass

    def stop(self):
        pass


@contextlib.asynccontextmanager
async def running(hello_timeout=5.0):
    switch = DeviceSwitch(NullMic(), NullPlayer())
    events: asyncio.Queue[str] = asyncio.Queue()
    app = create_app(
        token=TOKEN,
        switch=switch,
        events=events,
        current_state=lambda: "idle",
        hello_timeout=hello_timeout,
    )
    server = await start_server(app, "127.0.0.1", 0)
    try:
        yield f"ws://127.0.0.1:{server.port}/robot", switch, events
    finally:
        await asyncio.wait_for(server.stop(), 5)


async def next_event(events):
    return await asyncio.wait_for(events.get(), 2)


def test_good_hello_gets_welcome_and_state():
    async def run():
        async with running() as (url, switch, events):
            robot = FakeRobot(url, TOKEN)
            await robot.connect()
            assert await robot.recv() == {"type": "welcome"}
            assert await robot.recv() == {"type": "state", "name": "idle"}
            assert switch.robot is not None
            assert switch.robot.fw_version == "fake-robot"
            assert await next_event(events) == "reset"
            await robot.close()

    asyncio.run(run())


def test_bad_token_is_closed_4001_and_never_logged(caplog):
    caplog.set_level(logging.DEBUG)

    async def run():
        async with running() as (url, switch, _):
            robot = FakeRobot(url, "wrong-token-wrong-token")
            await robot.connect()
            assert await robot.recv() is None
            assert robot.close_code == CLOSE_BAD_TOKEN
            assert switch.robot is None

    asyncio.run(run())
    # The test's own client logs its frames at debug level; only the server side matters.
    server_logs = " ".join(
        r.getMessage() for r in caplog.records if not r.name.startswith("websockets.client")
    )
    assert "Robot refused: bad token" in server_logs
    assert "wrong-token" not in server_logs
    assert TOKEN not in server_logs


def test_non_ascii_token_is_closed_4001_and_the_server_keeps_running():
    async def run():
        async with running() as (url, switch, _):
            hostile = FakeRobot(url, "é" * 20)
            await hostile.connect()
            assert await hostile.recv() is None
            assert hostile.close_code == CLOSE_BAD_TOKEN
            robot = FakeRobot(url, TOKEN)
            await robot.connect()
            assert await robot.recv() == {"type": "welcome"}
            assert switch.robot is not None
            await robot.close()

    asyncio.run(run())


def test_no_hello_is_closed_4002():
    async def run():
        async with running(hello_timeout=0.2) as (url, _, _):
            robot = FakeRobot(url, TOKEN)
            await robot.connect(hello=False)
            assert await robot.recv() is None
            assert robot.close_code == CLOSE_NO_HELLO

    asyncio.run(run())


@pytest.mark.parametrize("first", [b"\x00\x00", '{"type": "button", "action": "down"}', "junk"])
def test_a_first_frame_that_is_not_hello_is_closed_4002(first):
    async def run():
        async with running() as (url, switch, events):
            robot = FakeRobot(url, TOKEN)
            await robot.connect(hello=False)
            await robot.ws.send(first)
            assert await robot.recv() is None
            assert robot.close_code == CLOSE_NO_HELLO
            assert switch.robot is None
            assert events.empty()

    asyncio.run(run())


def test_new_connection_replaces_old_with_4003():
    async def run():
        async with running() as (url, switch, _):
            old, new = FakeRobot(url, TOKEN), FakeRobot(url, TOKEN)
            await old.connect()
            await old.recv_until("state")
            first = switch.robot
            await new.connect()
            assert await new.recv() == {"type": "welcome"}
            assert await new.recv() == {"type": "state", "name": "idle"}
            assert await old.recv() is None
            assert old.close_code == CLOSE_REPLACED
            assert first.closed
            assert switch.robot is not None and switch.robot is not first
            await new.close()

    asyncio.run(run())


def test_disconnect_detaches_closes_the_link_and_sends_reset():
    async def run():
        async with running() as (url, switch, events):
            robot = FakeRobot(url, TOKEN)
            await robot.connect()
            await robot.recv_until("state")
            link = switch.robot
            assert await next_event(events) == "reset"
            await robot.close()
            assert await next_event(events) == "reset"
            assert switch.robot is None
            assert link.closed

    asyncio.run(run())


def test_buttons_and_audio_reach_the_events_and_the_mic():
    async def run():
        async with running() as (url, switch, events):
            robot = FakeRobot(url, TOKEN)
            await robot.connect()
            await robot.recv_until("state")
            assert await next_event(events) == "reset"
            await robot.send_audio(b"\x09\x00" * 10)  # before down: dropped
            await robot.press()
            assert await next_event(events) == "down"
            await robot.send_audio(b"\x01\x00" * 400)
            await robot.release()
            assert await next_event(events) == "up"
            assert switch.mic.stop() == b"\x01\x00" * 400
            await robot.close()

    asyncio.run(run())


def test_bad_messages_are_ignored_not_fatal():
    async def run():
        async with running() as (url, switch, events):
            robot = FakeRobot(url, TOKEN)
            await robot.connect()
            await robot.recv_until("state")
            await robot.ws.send("not json")
            await robot.send({"type": "dance"})
            await robot.send({"type": "hello", "token": TOKEN})  # a second hello: ignored
            await robot.send({"type": "touch"})
            await robot.send({"type": "stats", "data": {"rssi": -60}})
            await robot.press()
            assert await next_event(events) == "reset"
            assert await next_event(events) == "down"
            assert switch.robot is not None
            await robot.close()

    asyncio.run(run())


def test_oversized_frame_closes_only_that_connection():
    async def run():
        async with running() as (url, switch, events):
            robot = FakeRobot(url, TOKEN)
            await robot.connect()
            await robot.recv_until("state")
            assert await next_event(events) == "reset"
            await robot.ws.send(b"\x00" * (64 * 1024 + 2))
            assert await robot.recv() is None
            assert await next_event(events) == "reset"
            assert switch.robot is None
            again = FakeRobot(url, TOKEN)
            await again.connect()
            assert await again.recv() == {"type": "welcome"}
            await again.close()

    asyncio.run(run())


def test_busy_port_is_a_clear_error():
    async def run():
        async with running() as (url, _, _):
            port = int(url.rsplit(":", 1)[1].split("/")[0])
            app = create_app(
                token=TOKEN,
                switch=DeviceSwitch(NullMic(), NullPlayer()),
                events=asyncio.Queue(),
                current_state=lambda: "idle",
            )
            with pytest.raises(ServerStartError, match="Can't listen"):
                await start_server(app, "127.0.0.1", port)

    asyncio.run(run())
