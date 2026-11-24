"""The fake robot voice tool: its pure helpers, its playback thread, and one run against
a real server on 127.0.0.1 with fake audio devices and a fake key."""

import asyncio
import socket
import threading
import time

import pytest

import desk_buddy.fake_robot as fake_robot
from desk_buddy.config import Settings
from desk_buddy.fake_robot import FakeRobot, Playback, close_reason, handle_frame, robot_url
from desk_buddy.link.switch import DeviceSwitch
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
    assert "Ignored a bad frame from Buddy" in caplog.text


class SlowRobot:
    """Records what the tool sends; each audio send takes a while (like a slow network)."""

    def __init__(self):
        self.sent = []

    async def press(self):
        self.sent.append("down")

    async def release(self):
        self.sent.append("up")

    async def send_audio(self, pcm):
        await asyncio.sleep(0.05)
        self.sent.append(pcm)


class CountingMic:
    def __init__(self):
        self.drains = 0

    def start(self):
        pass

    def drain(self):
        self.drains += 1
        return b"d%d" % self.drains

    def stop(self):
        return b"rest"


def test_key_release_does_not_lose_a_chunk_being_sent():
    robot, mic = SlowRobot(), CountingMic()

    async def run():
        keys: asyncio.Queue[str] = asyncio.Queue()
        task = asyncio.create_task(fake_robot._handle_keys(keys, robot, mic))
        keys.put_nowait("down")
        await async_wait_until(lambda: mic.drains >= 1)  # a drain is being sent now
        keys.put_nowait("up")
        await async_wait_until(lambda: "up" in robot.sent)
        task.cancel()

    asyncio.run(run())
    assert robot.sent[0] == "down"
    assert robot.sent[-2:] == [b"rest", "up"]
    drained = [b"d%d" % n for n in range(1, mic.drains + 1)]
    assert robot.sent[1:-2] == drained  # every drained chunk was sent, in order


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
    monkeypatch.setattr(fake_robot, "KeyWatcher", FakeWatcher)
    assert fake_robot.main() == 1
    assert "Can't connect" in capsys.readouterr().err


class FakeMic:
    def __init__(self, *args, **kwargs):
        self.recording, self.closed = False, False

    def start(self):
        self.recording = True

    def drain(self):
        return b"\x01\x00" * 160 if self.recording else b""

    def stop(self):
        self.recording = False
        return b"\x02\x00" * 10

    def close(self):
        self.closed = True


class FakeWatcher:
    last = None

    def __init__(self, vk, on_event):
        self.on_event, self.stopped = on_event, False
        FakeWatcher.last = self

    def start(self):
        pass

    def stop(self):
        self.stopped = True


def test_a_run_against_a_real_server(monkeypatch):
    """Connect, hold the key for a moment, then the server stops: exit code 0."""
    monkeypatch.setattr(fake_robot, "Microphone", FakeMic)
    monkeypatch.setattr(fake_robot, "Speaker", FakeSpeaker)
    monkeypatch.setattr(fake_robot, "KeyWatcher", FakeWatcher)

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
            FakeWatcher.last.on_event("down")
            assert await asyncio.wait_for(events.get(), 10) == "down"
            await async_wait_until(lambda: frames)  # at least one streamed frame arrived
            FakeWatcher.last.on_event("up")
            assert await asyncio.wait_for(events.get(), 10) == "up"
            pcm = switch.mic.stop()
            assert pcm.endswith(b"\x02\x00" * 10)  # the final stop() is sent too
            assert b"\x01\x00" * 160 in pcm  # streamed while held
        finally:
            await asyncio.wait_for(server.stop(), 10)
        return await asyncio.wait_for(tool, 5)

    assert asyncio.run(run()) == 0
    assert FakeWatcher.last.stopped


def test_a_push_to_talk_failure_ends_the_tool_with_a_clear_message(monkeypatch, caplog):
    class BrokenMic(FakeMic):
        def start(self):
            raise OSError("mic unplugged")

    monkeypatch.setattr(fake_robot, "Microphone", BrokenMic)
    monkeypatch.setattr(fake_robot, "Speaker", FakeSpeaker)
    monkeypatch.setattr(fake_robot, "KeyWatcher", FakeWatcher)

    async def run():
        switch = DeviceSwitch(NullDevice(), NullDevice())
        events: asyncio.Queue[str] = asyncio.Queue()
        app = create_app(token=TOKEN, switch=switch, events=events, current_state=lambda: "idle")
        server = await start_server(app, "127.0.0.1", 0)
        settings = Settings(anthropic_api_key="k", robot_token=TOKEN, server_port=server.port)
        tool = asyncio.create_task(fake_robot.run(settings))
        try:
            assert await asyncio.wait_for(events.get(), 10) == "reset"
            FakeWatcher.last.on_event("down")
            return await asyncio.wait_for(tool, 10)  # ends by itself, server still up
        finally:
            await asyncio.wait_for(server.stop(), 10)

    assert asyncio.run(run()) == 1
    assert "Push-to-talk failed: mic unplugged" in caplog.text


class NullDevice:
    def start(self):
        pass

    def stop(self):
        return b""

    def current_token(self):
        return 0

    def play(self, pcm, token):
        pass
