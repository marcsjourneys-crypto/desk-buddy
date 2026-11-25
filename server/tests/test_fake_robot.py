"""The fake robot voice tool: its pure helpers, its playback thread, and one run against
a real server on 127.0.0.1 with fake audio devices and a fake key."""

import asyncio
import contextlib
import socket
import threading
import time

import pytest

import desk_buddy.fake_robot as fake_robot
from desk_buddy.config import Settings
from desk_buddy.fake_robot import FakeRobot, Playback, close_reason, handle_frame, robot_url
from desk_buddy.link.switch import DeviceSwitch
from desk_buddy.loop import State
from desk_buddy.server import CLOSE_BAD_TOKEN, CLOSE_REPLACED, create_app, start_server

TOKEN = "t" * 20


def test_robot_url_uses_loopback_for_wildcard_host():
    s = Settings(anthropic_api_key="k", server_host="0.0.0.0", server_port=9000)
    assert robot_url(s) == "ws://127.0.0.1:9000/robot"


@pytest.mark.parametrize("host", ["", "0.0.0.0", "::"])
def test_robot_url_maps_every_wildcard_host_to_loopback(host):
    s = Settings(anthropic_api_key="k", server_host=host)
    assert robot_url(s) == "ws://127.0.0.1:8765/robot"


def test_robot_url_keeps_a_specific_host():
    s = Settings(anthropic_api_key="k", server_host="192.168.1.5")
    assert robot_url(s) == "ws://192.168.1.5:8765/robot"


@pytest.mark.parametrize(
    ("code", "words", "exit_code"),
    [
        (CLOSE_BAD_TOKEN, "bad ROBOT_TOKEN", 2),
        (CLOSE_REPLACED, "replaced by another robot", 1),
        (4002, "no hello", 1),
        (1012, "code 1012", 0),
        (1000, "code 1000", 0),
        (None, "closed", 0),
        (1011, "code 1011", 1),
    ],
)
def test_close_reason(code, words, exit_code):
    message, result = close_reason(code)
    assert words in message
    assert result == exit_code


class FakeSpeaker:
    def __init__(self, block=None):
        self.gen, self.played, self.stops = 0, [], 0
        self.block = block  # an Event the first play waits on

    def current_token(self):
        return self.gen

    def play(self, pcm, token):
        if self.block is not None:
            self.block.wait(2)
            self.block = None
        if token == self.gen:
            self.played.append(pcm)

    def stop(self):
        self.stops += 1
        self.gen += 1
        if self.block is not None:
            self.block.set()  # like the real Speaker: stop() ends a play in progress

    def close(self):
        pass


def wait_until(condition, timeout=10.0):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.005)


async def async_wait_until(condition, timeout=10.0):
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.005)


def test_playback_plays_chunks_in_order_and_close_joins_the_thread():
    speaker = FakeSpeaker()
    playback = Playback(speaker)
    playback.feed(b"a")
    playback.feed(b"b")
    wait_until(lambda: len(speaker.played) == 2)
    playback.close()
    assert speaker.played == [b"a", b"b"]
    assert not playback.alive


def test_playback_close_stops_at_once_and_drops_queued_audio():
    release = threading.Event()
    speaker = FakeSpeaker(block=release)
    playback = Playback(speaker)
    playback.feed(b"first")  # the thread blocks on this one
    playback.feed(b"queued")
    started = time.monotonic()
    playback.close()
    assert time.monotonic() - started < 1
    assert not playback.alive
    assert speaker.played == []


def test_playback_stop_drops_queued_audio():
    release = threading.Event()
    speaker = FakeSpeaker(block=release)
    playback = Playback(speaker)
    playback.feed(b"first")  # the thread blocks on this one
    playback.feed(b"queued")
    playback.stop()
    playback.feed(b"after")
    wait_until(lambda: speaker.played == [b"after"])
    playback.close()
    assert speaker.played == [b"after"]


def test_playback_close_warns_if_the_thread_does_not_finish(caplog):
    class StuckSpeaker(FakeSpeaker):
        def play(self, pcm, token):
            stuck.wait(10)  # ignores stop()

    stuck = threading.Event()
    playback = Playback(StuckSpeaker(), join_timeout=0.1)
    playback.feed(b"a")
    wait_until(lambda: playback._queue.empty())
    playback.close()
    assert "Playback thread did not stop" in caplog.text
    stuck.set()
    wait_until(lambda: not playback.alive)


def test_malformed_json_from_the_server_is_skipped(caplog):
    class FakeConnection:
        def __init__(self, frames):
            self.frames = list(frames)

        async def recv(self):
            return self.frames.pop(0)

    robot = FakeRobot("ws://unused", TOKEN)
    robot.ws = FakeConnection(["{not json", '{"type": "welcome"}'])
    assert asyncio.run(robot.recv()) == {"type": "welcome"}
    assert "Ignored a bad frame from CARL" in caplog.text


class AudioRobot:
    """Records the audio the tool sends."""

    def __init__(self):
        self.sent = []

    async def send_audio(self, pcm):
        self.sent.append(pcm)


class CountingMic:
    def __init__(self):
        self.drains, self.calls = 0, []

    def start(self):
        self.calls.append("start")

    def drain(self):
        self.drains += 1
        return b"d%d" % self.drains

    def stop(self):
        self.calls.append("stop")
        return b"rest"


def test_the_mic_streams_only_while_buddy_is_listening():
    robot, mic = AudioRobot(), CountingMic()

    async def run():
        streamer = fake_robot.MicStreamer(robot, mic)
        task = asyncio.create_task(streamer.run())
        streamer.on_state("idle")
        await asyncio.sleep(0.1)
        assert robot.sent == [] and mic.calls == []
        streamer.on_state("listening")
        await async_wait_until(lambda: len(robot.sent) >= 2)
        streamer.on_state("thinking")
        await async_wait_until(lambda: mic.calls == ["start", "stop"])
        sent = len(robot.sent)
        await asyncio.sleep(0.1)
        assert len(robot.sent) == sent  # stopped streaming
        streamer.on_state("listening")  # the next turn streams again
        await async_wait_until(lambda: len(robot.sent) > sent)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert robot.sent == [b"d%d" % n for n in range(1, len(robot.sent) + 1)]  # in order
    assert mic.calls == ["start", "stop", "start", "stop"]  # cancelling stops the mic too


class RecordingPlayback:
    def __init__(self):
        self.fed, self.stops = [], 0

    def feed(self, chunk):
        self.fed.append(chunk)

    def stop(self):
        self.stops += 1


def test_handle_frame(caplog):
    caplog.set_level("INFO")
    playback = RecordingPlayback()
    handle_frame(b"\x01\x00", playback)
    handle_frame({"type": "state", "name": "thinking"}, playback)
    handle_frame({"type": "emotion", "name": "happy"}, playback)
    handle_frame({"type": "audio_stop"}, playback)
    handle_frame({"type": "something_new"}, playback)  # ignored
    assert playback.fed == [b"\x01\x00"]
    assert playback.stops == 1
    assert "Robot state: thinking" in caplog.text
    assert "Robot emotion: happy" in caplog.text


def test_main_without_token_is_a_clear_error(monkeypatch, capsys):
    monkeypatch.setattr(fake_robot, "load_settings", lambda: Settings(anthropic_api_key="k"))
    assert fake_robot.main() == 2
    assert "ROBOT_TOKEN" in capsys.readouterr().err


def test_main_cannot_connect_is_a_clear_error(monkeypatch, capsys):
    # Nothing listens on this port: a loopback port that was bound, then freed.
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    monkeypatch.setattr(
        fake_robot,
        "load_settings",
        lambda: Settings(anthropic_api_key="k", robot_token=TOKEN, server_port=port),
    )
    monkeypatch.setattr(fake_robot, "Microphone", FakeMic)
    monkeypatch.setattr(fake_robot, "Speaker", FakeSpeaker)
    assert fake_robot.main() == 1
    assert "Can't connect" in capsys.readouterr().err


class FakeMic:
    last = None

    def __init__(self, *args, **kwargs):
        self.recording, self.closed = False, False
        FakeMic.last = self

    def start(self):
        self.recording = True

    def drain(self):
        return b"\x01\x00" * 160 if self.recording else b""

    def stop(self):
        self.recording = False
        return b"\x02\x00" * 10

    def close(self):
        self.closed = True


def test_a_run_against_a_real_server(monkeypatch, caplog):
    """Connect, stream while CARL is listening, then the server stops: exit code 0."""
    caplog.set_level("INFO")
    monkeypatch.setattr(fake_robot, "Microphone", FakeMic)
    monkeypatch.setattr(fake_robot, "Speaker", FakeSpeaker)

    class Null:
        def start(self):
            pass

        def stop(self):
            return b""

        def current_token(self):
            return 0

        def play(self, pcm, token):
            pass

    async def run():
        switch = DeviceSwitch(Null(), Null())
        events: asyncio.Queue[str] = asyncio.Queue()
        app = create_app(token=TOKEN, switch=switch, events=events, current_state=lambda: "idle")
        server = await start_server(app, "127.0.0.1", 0)
        settings = Settings(anthropic_api_key="k", robot_token=TOKEN, server_port=server.port)
        tool = asyncio.create_task(fake_robot.run(settings))
        try:
            assert await asyncio.wait_for(events.get(), 10) == "reset"
            link = switch.robot
            frames = []
            feed = link.mic.feed
            link.mic.feed = lambda data: (frames.append(data), feed(data))
            await asyncio.sleep(0.1)
            assert frames == []  # idle: nothing streams
            link.mic.begin()  # what the PC key does on the server
            switch.send_state(State.LISTENING)
            await async_wait_until(lambda: frames)  # at least one streamed frame arrived
            switch.send_state(State.THINKING)
            await async_wait_until(lambda: not FakeMic.last.recording)
            link.mic.end()
            assert b"\x01\x00" * 160 in switch.mic.stop()  # streamed while listening
        finally:
            await asyncio.wait_for(server.stop(), 10)
        return await asyncio.wait_for(tool, 5)

    assert asyncio.run(run()) == 0
    assert "Hold right_ctrl in the CARL window to talk" in caplog.text


def test_a_mic_failure_ends_the_tool_with_a_clear_message(monkeypatch, caplog):
    class BrokenMic(FakeMic):
        def start(self):
            raise OSError("mic unplugged")

    monkeypatch.setattr(fake_robot, "Microphone", BrokenMic)
    monkeypatch.setattr(fake_robot, "Speaker", FakeSpeaker)

    async def run():
        switch = DeviceSwitch(NullDevice(), NullDevice())
        events: asyncio.Queue[str] = asyncio.Queue()
        app = create_app(token=TOKEN, switch=switch, events=events, current_state=lambda: "idle")
        server = await start_server(app, "127.0.0.1", 0)
        settings = Settings(anthropic_api_key="k", robot_token=TOKEN, server_port=server.port)
        tool = asyncio.create_task(fake_robot.run(settings))
        try:
            assert await asyncio.wait_for(events.get(), 10) == "reset"
            switch.send_state(State.LISTENING)
            return await asyncio.wait_for(tool, 10)  # ends by itself, server still up
        finally:
            await asyncio.wait_for(server.stop(), 10)

    assert asyncio.run(run()) == 1
    assert "Mic streaming failed: mic unplugged" in caplog.text


class NullDevice:
    def start(self):
        pass

    def stop(self):
        return b""

    def current_token(self):
        return 0

    def play(self, pcm, token):
        pass
