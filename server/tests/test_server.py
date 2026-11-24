"""The robot WebSocket route, served by a real uvicorn on 127.0.0.1 with a free port."""

import asyncio
import contextlib
import logging
import time

import pytest

import desk_buddy.server as server_module
from desk_buddy.audio import BYTES_PER_SECOND
from desk_buddy.brain.memory import Conversation
from desk_buddy.fake_robot import FakeRobot
from desk_buddy.link.robot import RobotLink
from desk_buddy.link.switch import DeviceSwitch
from desk_buddy.loop import Orchestrator, State
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


def test_unencodable_token_is_closed_4002_cleanly(caplog):
    # A lone surrogate survives json.loads but can't be UTF-8 encoded: the hello is
    # malformed, so it is treated like no hello (4002), not as a wrong token.
    async def run():
        async with running() as (url, switch, _):
            hostile = FakeRobot(url, TOKEN)
            await hostile.connect(hello=False)
            await hostile.ws.send('{"type": "hello", "token": "\\ud800"}')
            assert await hostile.recv() is None
            assert hostile.close_code == CLOSE_NO_HELLO
            robot = FakeRobot(url, TOKEN)
            await robot.connect()
            assert await robot.recv() == {"type": "welcome"}
            await robot.close()

    asyncio.run(run())
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_stopping_the_server_closes_a_connected_robot_quickly():
    async def run():
        switch = DeviceSwitch(NullMic(), NullPlayer())
        app = create_app(
            token=TOKEN, switch=switch, events=asyncio.Queue(), current_state=lambda: "idle"
        )
        server = await start_server(app, "127.0.0.1", 0)
        robot = FakeRobot(f"ws://127.0.0.1:{server.port}/robot", TOKEN)
        await robot.connect()
        await robot.recv_until("state")
        started = time.monotonic()
        await asyncio.wait_for(server.stop(), 10)
        assert time.monotonic() - started < 3
        assert await robot.recv(timeout=2) is None
        assert switch.robot is None

    asyncio.run(run())


def test_a_closed_link_ignores_a_frame_that_arrives_late():
    events: asyncio.Queue[str] = asyncio.Queue()

    class LateWebSocket:
        """Its first receive() returns a button press after the link was replaced."""

        def __init__(self):
            self.link = None

        async def receive(self):
            await self.link.close(CLOSE_REPLACED)
            return {"type": "websocket.receive", "text": '{"type": "button", "action": "down"}'}

        async def close(self, code=1000):
            pass

    async def run():
        ws = LateWebSocket()
        ws.link = RobotLink(ws, asyncio.get_running_loop())
        await asyncio.wait_for(server_module._receive_loop(ws, ws.link, events), 2)

    asyncio.run(run())
    assert events.empty()


def test_a_failed_startup_cancels_serving_and_closes_the_socket(monkeypatch):
    seen = {}

    async def broken_serve(self, sockets=None):
        seen["sockets"] = sockets
        raise RuntimeError("boom")

    monkeypatch.setattr(server_module._Server, "serve", broken_serve)

    async def run():
        app = create_app(
            token=TOKEN,
            switch=DeviceSwitch(NullMic(), NullPlayer()),
            events=asyncio.Queue(),
            current_state=lambda: "idle",
        )
        with pytest.raises(RuntimeError, match="boom"):
            await start_server(app, "127.0.0.1", 0)

    asyncio.run(run())
    assert seen["sockets"][0].fileno() == -1  # closed


def test_a_cancelled_startup_cancels_serving_and_closes_the_socket(monkeypatch):
    seen = {"cancelled": False}

    async def stuck_serve(self, sockets=None):
        seen["sockets"] = sockets
        try:
            await asyncio.sleep(60)  # never sets started
        except asyncio.CancelledError:
            seen["cancelled"] = True
            raise

    monkeypatch.setattr(server_module._Server, "serve", stuck_serve)

    async def run():
        app = create_app(
            token=TOKEN,
            switch=DeviceSwitch(NullMic(), NullPlayer()),
            events=asyncio.Queue(),
            current_state=lambda: "idle",
        )
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(start_server(app, "127.0.0.1", 0), 0.2)

    asyncio.run(run())
    assert seen["cancelled"]
    assert seen["sockets"][0].fileno() == -1


# --- A real Orchestrator (fake STT, LLM and TTS) behind the server ---


class EchoTranscriber:
    def __init__(self):
        self.heard = []

    def transcribe(self, pcm):
        self.heard.append(len(pcm))
        return "hello buddy"


class OneLineChat:
    async def stream_reply(self, history, text):
        yield "[happy] Hi there. "
        yield "Nice to see you."


class ToneSynth:
    def __init__(self, seconds=0.1):
        self.bytes = int(seconds * BYTES_PER_SECOND) & ~1

    def synthesize(self, text):
        return b"\x01\x00" * (self.bytes // 2)


@contextlib.asynccontextmanager
async def buddy(synth=None):
    switch = DeviceSwitch(NullMic(), NullPlayer())
    events: asyncio.Queue[str] = asyncio.Queue()
    transcriber = EchoTranscriber()
    orch = Orchestrator(
        mic=switch.mic,
        transcriber=transcriber,
        chat=OneLineChat(),
        synthesizer=synth or ToneSynth(),
        speaker=switch.player,
        conversation=Conversation(),
        on_state=switch.send_state,
        on_emotion=switch.send_emotion,
    )
    app = create_app(
        token=TOKEN, switch=switch, events=events, current_state=lambda: orch.state.value
    )
    server = await start_server(app, "127.0.0.1", 0)
    loop_task = asyncio.create_task(orch.run(events))
    try:
        yield f"ws://127.0.0.1:{server.port}/robot", orch, switch, transcriber
    finally:
        loop_task.cancel()
        await orch.shutdown()
        await asyncio.wait_for(server.stop(), 5)


IDLE = {"type": "state", "name": "idle"}
SPEAKING = {"type": "state", "name": "speaking"}
LISTENING = {"type": "state", "name": "listening"}


def text_frames(frames):
    return [f for f in frames if isinstance(f, dict)]


async def recv_until_frame(robot, wanted, timeout=5.0):
    """Frames up to and including the first one equal to `wanted`."""
    frames = []
    async with asyncio.timeout(timeout):
        while True:
            frame = await robot.recv()
            assert frame is not None, f"closed ({robot.close_code}) before {wanted}"
            frames.append(frame)
            if frame == wanted:
                return frames


async def start_a_reply(robot):
    """Connect, talk for 0.5 s, and return once Buddy is speaking and audio is arriving."""
    await robot.connect()
    await robot.recv_until("state")
    await robot.press()
    await robot.send_audio(b"\x00\x00" * (BYTES_PER_SECOND // 4))  # 0.5 s
    await robot.release()
    await recv_until_frame(robot, SPEAKING)
    assert isinstance(await robot.recv(), dict)  # audio_start
    assert isinstance(await robot.recv(), bytes)


async def wait_for(condition, timeout=2.0):
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)


def test_full_turn_through_the_robot():
    async def run():
        async with buddy() as (url, orch, _, transcriber):
            robot = FakeRobot(url, TOKEN)
            await robot.connect()
            await robot.recv_until("state")
            await robot.press()
            await robot.send_audio(b"\x00\x00" * (BYTES_PER_SECOND // 4))  # 0.5 s
            await robot.release()
            frames = await recv_until_frame(robot, IDLE)
            texts = text_frames(frames)
            kinds = [(t["type"], t.get("name")) for t in texts]
            assert kinds[0] == ("state", "listening")
            assert kinds.index(("state", "thinking")) < kinds.index(("emotion", "happy"))
            assert kinds.index(("emotion", "happy")) < kinds.index(("state", "speaking"))
            assert [t["type"] for t in texts[4:]] == [
                "audio_start",
                "audio_end",
                "audio_start",
                "audio_end",
                "state",
            ]
            audio = b"".join(f for f in frames if isinstance(f, bytes))
            assert len(audio) == 2 * (BYTES_PER_SECOND // 10)  # two 0.1 s sentences
            assert transcriber.heard == [BYTES_PER_SECOND // 2]  # 0.5 s of int16
            assert orch.state is State.IDLE
            await robot.close()

    asyncio.run(run())


def test_button_press_mid_reply_sends_audio_stop():
    async def run():
        async with buddy(ToneSynth(2.0)) as (url, orch, _, _):
            robot = FakeRobot(url, TOKEN)
            await start_a_reply(robot)
            await robot.press()
            frames = await recv_until_frame(robot, LISTENING)
            texts = text_frames(frames)
            assert {"type": "audio_stop"} in texts
            assert {"type": "audio_end"} not in texts
            await robot.release()  # no audio: an ignored tap
            await recv_until_frame(robot, IDLE)
            # Nothing more arrives: the interrupted reply is not resumed.
            with pytest.raises(TimeoutError):
                await robot.recv(timeout=0.5)
            assert orch.state is State.IDLE
            await robot.close()

    started = time.monotonic()
    asyncio.run(run())
    # The 2 s sentences were cut short: the playing worker thread stopped promptly too
    # (asyncio.run waits for it).
    assert time.monotonic() - started < 3.5


def test_disconnect_mid_reply_returns_to_idle_on_the_pc():
    async def run():
        async with buddy(ToneSynth(10.0)) as (url, orch, switch, _):
            robot = FakeRobot(url, TOKEN)
            await start_a_reply(robot)
            link = switch.robot
            await robot.close()
            await wait_for(lambda: orch.state is State.IDLE and switch.robot is None)
            assert link.closed

    started = time.monotonic()
    asyncio.run(run())
    assert time.monotonic() - started < 5  # the 10 s sentence stopped playing


def test_a_new_robot_mid_reply_closes_the_old_one_and_goes_idle():
    async def run():
        async with buddy(ToneSynth(10.0)) as (url, orch, switch, _):
            old = FakeRobot(url, TOKEN)
            await start_a_reply(old)
            old_link = switch.robot
            new = FakeRobot(url, TOKEN)
            await new.connect()
            assert await new.recv() == {"type": "welcome"}
            # The old robot is closed with 4003, and its audio stops.
            async with asyncio.timeout(2):
                while await old.recv() is not None:
                    pass
            assert old.close_code == CLOSE_REPLACED
            assert old_link.closed
            # The new robot sees Buddy go idle (the reply is interrupted, not moved over).
            await recv_until_frame(new, IDLE)
            await wait_for(lambda: orch.state is State.IDLE)
            assert switch.robot is not None and switch.robot is not old_link
            await new.close()

    started = time.monotonic()
    asyncio.run(run())
    assert time.monotonic() - started < 5  # the old link's 10 s play stopped
