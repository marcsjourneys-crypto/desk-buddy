"""The fake robot voice tool: its pure helpers, its playback thread, and one run against
a real server on 127.0.0.1 with fake audio devices and a fake key."""

import asyncio
import socket
import threading
import time

import pytest

import desk_buddy.fake_robot as fake_robot
from desk_buddy.config import Settings
from desk_buddy.fake_robot import Playback, close_reason, handle_frame, robot_url
from desk_buddy.link.switch import DeviceSwitch
from desk_buddy.server import CLOSE_BAD_TOKEN, CLOSE_REPLACED, create_app, start_server

TOKEN = "t" * 20


def test_robot_url_uses_loopback_for_wildcard_host():
    s = Settings(anthropic_api_key="k", server_host="0.0.0.0", server_port=9000)
    assert robot_url(s) == "ws://127.0.0.1:9000/robot"


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


def wait_until(condition, timeout=2.0):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.005)


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
            assert await asyncio.wait_for(events.get(), 5) == "reset"
            FakeWatcher.last.on_event("down")
            assert await asyncio.wait_for(events.get(), 2) == "down"
            await asyncio.sleep(0.1)  # a few 20 ms drains
            FakeWatcher.last.on_event("up")
            assert await asyncio.wait_for(events.get(), 2) == "up"
            pcm = switch.mic.stop()
            assert pcm.endswith(b"\x02\x00" * 10)  # the final stop() is sent too
            assert b"\x01\x00" * 160 in pcm  # streamed while held
        finally:
            await asyncio.wait_for(server.stop(), 10)
        return await asyncio.wait_for(tool, 5)

    assert asyncio.run(run()) == 0
    assert FakeWatcher.last.stopped
