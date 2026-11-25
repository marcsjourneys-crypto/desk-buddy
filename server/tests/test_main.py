import asyncio
import logging
import time

import pytest

import desk_buddy.__main__ as entry
from desk_buddy.audio import AudioDeviceError
from desk_buddy.config import Settings
from desk_buddy.link.switch import DeviceSwitch, SwitchedMic, SwitchedPlayer
from desk_buddy.server import ServerStartError

TOKEN = "s3cret-token-1234567"


@pytest.fixture
def calls():
    return []


@pytest.fixture
def fakes(monkeypatch, calls):
    """Patch every device and model in __main__ with fakes that record their closing."""

    class FakeTranscriber:
        def __init__(self, *args, **kwargs):
            calls.append("transcriber created")

    class FakeSynthesizer:
        def __init__(self):
            calls.append("synthesizer created")

        def close(self):
            calls.append("synthesizer closed")

    class FakeMicrophone:
        def __init__(self, *args, **kwargs):
            calls.append("mic created")

        def close(self):
            calls.append("mic closed")

    class FakeSpeaker:
        def __init__(self, *args, **kwargs):
            calls.append("speaker created")

        def close(self):
            calls.append("speaker closed")

    class FakeOrchestrator:
        def __init__(self, **kwargs):
            calls.append(("orchestrator kwargs", kwargs))

        async def run(self, events):
            calls.append(("events at run", list(events._queue), events))
            raise KeyboardInterrupt  # what Ctrl+C looks like inside the loop

        async def farewell(self, timeout):
            calls.append(("farewell timeout", timeout))
            calls.append("farewell")

        async def shutdown(self):
            calls.append("orchestrator shutdown")

    class FakeWatcher:
        def __init__(self, vk, on_event):
            pass

        def start(self):
            calls.append("watcher started")

        def stop(self):
            calls.append("watcher stopped")

    monkeypatch.setattr(entry, "load_settings", lambda: Settings(anthropic_api_key="k"))
    monkeypatch.setattr(entry, "check_audio_devices", lambda: None)
    monkeypatch.setattr(entry, "WhisperTranscriber", FakeTranscriber)
    monkeypatch.setattr(entry, "SapiSynthesizer", FakeSynthesizer)
    monkeypatch.setattr(entry, "Microphone", FakeMicrophone)
    monkeypatch.setattr(entry, "Speaker", FakeSpeaker)
    monkeypatch.setattr(entry, "create_chat", lambda settings, tools=None: object())
    monkeypatch.setattr(entry, "Orchestrator", FakeOrchestrator)
    monkeypatch.setattr(entry, "KeyWatcher", FakeWatcher)

    class FakeServer:
        port = 8765

        async def stop(self):
            calls.append("server stopped")

    def fake_create_app(**kwargs):
        calls.append(("create_app", kwargs))
        return "app"

    async def fake_start_server(app, host, port):
        calls.append(("start_server", app, host, port))
        return FakeServer()

    monkeypatch.setattr(entry, "create_app", fake_create_app)
    monkeypatch.setattr(entry, "start_server", fake_start_server)


def steps(calls):
    """The plain string entries (created/closed/started/stopped)."""
    return [c for c in calls if isinstance(c, str)]


def _raise(exc):
    def factory(*args, **kwargs):
        raise exc

    return factory


def test_mic_failure_prints_reason_and_closes_what_was_built(monkeypatch, capsys, fakes, calls):
    monkeypatch.setattr(entry, "Microphone", _raise(AudioDeviceError("no mic")))

    assert entry.main() == 2

    err = capsys.readouterr().err
    assert "Can't start" in err
    assert "no mic" in err
    assert steps(calls) == ["transcriber created", "synthesizer created", "synthesizer closed"]


def test_whisper_failure_is_a_clear_startup_error(monkeypatch, capsys, fakes, calls):
    monkeypatch.setattr(entry, "WhisperTranscriber", _raise(ValueError("bad model")))

    assert entry.main() == 2

    err = capsys.readouterr().err
    assert "Can't start: Can't load Whisper model 'base.en' on cpu (int8): bad model" in err
    assert "Traceback" not in err
    assert calls == []  # nothing was built before it, so nothing to close


def test_sapi_failure_is_a_clear_startup_error(monkeypatch, capsys, fakes, calls):
    monkeypatch.setattr(entry, "SapiSynthesizer", _raise(OSError("no voice")))

    assert entry.main() == 2

    err = capsys.readouterr().err
    assert "Can't start: Can't start Windows SAPI voice: no voice" in err
    assert "Traceback" not in err
    assert calls == ["transcriber created"]  # the transcriber has nothing to close


def test_ctrl_c_shuts_down_in_reverse_creation_order(fakes, calls):
    assert entry.main() == 0

    assert steps(calls) == [
        "transcriber created",
        "synthesizer created",
        "mic created",
        "speaker created",
        "watcher started",
        "farewell",
        "watcher stopped",
        "orchestrator shutdown",
        "speaker closed",
        "mic closed",
        "synthesizer closed",
    ]


def test_ready_queues_the_startup_greeting(fakes, calls):
    entry.main()
    ((_, queued, _),) = [c for c in calls if isinstance(c, tuple) and c[0] == "events at run"]
    assert queued == ["greet"]


def test_farewell_is_bounded_to_four_seconds_and_replaces_bye(fakes, calls, caplog):
    caplog.set_level(logging.INFO)
    entry.main()
    assert ("farewell timeout", 4.0) in calls
    assert "Bye" not in caplog.messages


@pytest.mark.parametrize("failure", ["hang", "raise"])
def test_a_failed_farewell_does_not_block_shutdown(monkeypatch, fakes, calls, failure):
    monkeypatch.setattr(entry, "FAREWELL_TIMEOUT", 0.05)
    orchestrator_class = entry.Orchestrator

    class BadFarewell(orchestrator_class):
        async def farewell(self, timeout):
            if failure == "hang":
                await asyncio.sleep(60)  # ignores its own timeout
            raise RuntimeError("no voice")

    monkeypatch.setattr(entry, "Orchestrator", BadFarewell)
    started = time.monotonic()
    assert entry.main() == 0
    assert time.monotonic() - started < 5
    assert steps(calls)[-4:] == [
        "orchestrator shutdown",
        "speaker closed",
        "mic closed",
        "synthesizer closed",
    ]


def test_robot_link_off_without_token(fakes, calls, caplog):
    caplog.set_level(logging.INFO)
    assert entry.main() == 0
    assert not [c for c in calls if isinstance(c, tuple) and c[0] == "start_server"]
    assert "Robot link: off (no ROBOT_TOKEN)" in caplog.text


def test_robot_link_on_with_token(monkeypatch, fakes, calls, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(
        entry, "load_settings", lambda: Settings(anthropic_api_key="k", robot_token=TOKEN)
    )

    assert entry.main() == 0

    assert ("start_server", "app", "127.0.0.1", 8765) in calls
    (app_kwargs,) = [c[1] for c in calls if isinstance(c, tuple) and c[0] == "create_app"]
    assert app_kwargs["token"] == TOKEN
    assert isinstance(app_kwargs["switch"], DeviceSwitch)
    # The server starts after the orchestrator is built and before the watcher.
    names = [c if isinstance(c, str) else c[0] for c in calls]
    assert names.index("orchestrator kwargs") < names.index("start_server")
    assert names.index("start_server") < names.index("watcher started")
    shutdown = steps(calls)
    assert shutdown[shutdown.index("farewell") :][:4] == [
        "farewell",  # before the server and the devices close
        "watcher stopped",
        "server stopped",
        "orchestrator shutdown",
    ]
    # A robot connecting asks for a greeting, after its reset.
    ((_, _, events),) = [c for c in calls if isinstance(c, tuple) and c[0] == "events at run"]
    app_kwargs["on_connect"]()
    assert list(events._queue)[-1] == "greet"
    assert "Robot link: on (ws://127.0.0.1:8765/robot)" in caplog.text
    assert TOKEN not in caplog.text


@pytest.mark.parametrize("host", ["0.0.0.0", "::", ""])
def test_a_wildcard_host_logs_the_lan_address(monkeypatch, fakes, calls, caplog, host):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(
        entry,
        "load_settings",
        lambda: Settings(anthropic_api_key="k", robot_token=TOKEN, server_host=host),
    )
    monkeypatch.setattr(entry, "lan_ip", lambda: "192.168.1.20")

    assert entry.main() == 0

    assert (
        "Robot link: on (ws://192.168.1.20:8765/robot — set the robot with: "
        "server 192.168.1.20:8765)" in caplog.text
    )
    assert TOKEN not in caplog.text


def test_a_wildcard_host_without_a_lan_address_logs_the_host(monkeypatch, fakes, calls, caplog):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(
        entry,
        "load_settings",
        lambda: Settings(anthropic_api_key="k", robot_token=TOKEN, server_host="0.0.0.0"),
    )
    monkeypatch.setattr(entry, "lan_ip", lambda: None)

    assert entry.main() == 0

    assert "Robot link: on (ws://0.0.0.0:8765/robot)" in caplog.text


def test_lan_ip_reads_the_outgoing_interface(monkeypatch):
    class FakeSocket:
        def __init__(self, family, kind):
            assert kind == entry.socket.SOCK_DGRAM

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def connect(self, address):
            assert address == ("192.0.2.1", 80)

        def getsockname(self):
            return ("192.168.1.20", 50000)

    monkeypatch.setattr(entry.socket, "socket", FakeSocket)
    assert entry.lan_ip() == "192.168.1.20"


def test_lan_ip_is_none_without_a_network(monkeypatch):
    class NoNetwork:
        def __init__(self, family, kind):
            raise OSError("network unreachable")

    monkeypatch.setattr(entry.socket, "socket", NoNetwork)
    assert entry.lan_ip() is None


def test_the_orchestrator_uses_the_device_switch(fakes, calls):
    entry.main()
    (kwargs,) = [c[1] for c in calls if isinstance(c, tuple) and c[0] == "orchestrator kwargs"]
    assert isinstance(kwargs["mic"], SwitchedMic)
    assert isinstance(kwargs["speaker"], SwitchedPlayer)


def test_busy_port_is_a_clear_startup_error(monkeypatch, capsys, fakes, calls):
    monkeypatch.setattr(
        entry, "load_settings", lambda: Settings(anthropic_api_key="k", robot_token=TOKEN)
    )

    async def busy(app, host, port):
        raise ServerStartError("Can't listen on 127.0.0.1:8765: in use")

    monkeypatch.setattr(entry, "start_server", busy)

    assert entry.main() == 2

    err = capsys.readouterr().err
    assert "Can't start: Can't listen on 127.0.0.1:8765: in use" in err
    assert "Traceback" not in err
    assert steps(calls) == [
        "transcriber created",
        "synthesizer created",
        "mic created",
        "speaker created",
        "orchestrator shutdown",
        "speaker closed",
        "mic closed",
        "synthesizer closed",
    ]


class Device:
    def start(self):
        pass

    def stop(self):
        return b""


class RecordingRobotMic(Device):
    def __init__(self, log):
        self.log = log

    def begin(self):
        self.log.append("begin")

    def end(self):
        self.log.append("end")


class KeyRobot:
    """A robot whose mic records begin/end into a shared log."""

    def __init__(self, log):
        self.mic = RecordingRobotMic(log)
        self.player = None

    def send(self, item):
        pass


DELAY = 0.05


def drain(events):
    out = []
    while not events.empty():
        out.append(events.get_nowait())
    return out


class LoggingQueue(asyncio.Queue):
    """Records each queued event in the same log as the mic calls, in call order."""

    def __init__(self, log):
        super().__init__()
        self.log = log

    def put_nowait(self, item):
        self.log.append(item)
        super().put_nowait(item)


def key_setup(log):
    switch = DeviceSwitch(Device(), Device())
    events = LoggingQueue(log)
    return switch, events, entry.key_handler(switch, events, delay=DELAY)


async def until(condition, timeout=2.0):
    """Poll until `condition()` is true, or fail after `timeout` seconds."""
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.005)


def test_the_key_goes_straight_to_the_events_without_a_robot():
    async def run():
        switch = DeviceSwitch(Device(), Device())
        events: asyncio.Queue[str] = asyncio.Queue()
        on_key = entry.key_handler(switch, events, delay=DELAY)
        on_key("down")
        on_key("up")
        return drain(events)

    assert asyncio.run(run()) == ["down", "up"]


def test_key_down_with_a_robot_begins_its_mic_then_queues_down():
    log = []

    async def run():
        switch, events, press = key_setup(log)
        switch.attach(KeyRobot(log))
        press("down")

    asyncio.run(run())
    assert log == ["begin", "down"]


def test_key_up_with_a_robot_ends_its_mic_after_the_delay():
    log = []

    async def run():
        switch, events, press = key_setup(log)
        switch.attach(KeyRobot(log))
        press("down")
        press("up")
        assert log == ["begin", "down"]  # nothing yet: in-flight audio may still arrive
        await until(lambda: "up" in log)
        assert drain(events) == ["down", "up"]

    asyncio.run(run())
    assert log == ["begin", "down", "end", "up"]


def test_a_re_press_during_the_delay_releases_first():
    log = []

    async def run():
        switch, events, press = key_setup(log)
        switch.attach(KeyRobot(log))
        press("down")
        press("up")
        press("down")
        assert log == ["begin", "down", "end", "up", "begin", "down"]
        # Release again and wait for that release. Had the cancelled first release also
        # fired (it was due earlier), an extra "up" would show up before this one.
        press("up")
        await until(lambda: log.count("up") >= 2)

    asyncio.run(run())
    assert log == ["begin", "down", "end", "up", "begin", "down", "end", "up"]


def test_a_double_press_within_the_delay_drops_the_first_clip():
    """Deliberate: a re-press within the delay ends and discards clip 1 (the robot mic's
    begin() clears its buffer before the orchestrator takes it), so it counts as a tap."""
    from desk_buddy.link.robot import RobotMic

    class Robot:
        def __init__(self):
            self.mic = RobotMic()
            self.player = None

        def send(self, item):
            pass

    robot = Robot()

    async def run():
        switch = DeviceSwitch(Device(), Device())
        events: asyncio.Queue[str] = asyncio.Queue()
        on_key = entry.key_handler(switch, events, delay=DELAY)
        switch.attach(robot)
        on_key("down")
        robot.mic.feed(b"\x01\x00" * 100)  # clip 1
        on_key("up")
        on_key("down")  # within the delay
        # The orchestrator handles the queued events (it runs on the same loop).
        assert drain(events) == ["down", "up", "down"]
        first = robot.mic.stop()  # its stop() on that "up"
        robot.mic.feed(b"\x02\x00" * 100)  # clip 2
        on_key("up")
        await until(lambda: not events.empty())
        assert drain(events) == ["up"]
        return first, robot.mic.stop()

    first, second = asyncio.run(run())
    assert first == b""  # clip 1 was cleared by the re-press: an ignored tap
    assert second == b"\x02\x00" * 100


def test_a_robot_detaching_during_the_delay_still_queues_up():
    log = []

    async def run():
        switch, events, press = key_setup(log)
        robot = KeyRobot(log)
        switch.attach(robot)
        press("down")
        press("up")
        switch.detach(robot)
        await until(lambda: "up" in log)

    asyncio.run(run())
    assert log == ["begin", "down", "up"]  # no end() on the old robot


def test_the_key_with_a_robot_attached_mid_press():
    log = []

    async def run():
        switch, events, press = key_setup(log)
        press("down")  # no robot yet: straight through
        switch.attach(KeyRobot(log))
        press("up")
        await until(lambda: "up" in log)

    asyncio.run(run())
    assert log == ["down", "end", "up"]
