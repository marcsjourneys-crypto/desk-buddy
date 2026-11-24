import asyncio
import logging

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
            raise KeyboardInterrupt  # what Ctrl+C looks like inside the loop

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
        "watcher stopped",
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
    assert shutdown[shutdown.index("watcher stopped") :][:3] == [
        "watcher stopped",
        "server stopped",
        "orchestrator shutdown",
    ]
    assert "Robot link: on (ws://127.0.0.1:8765/robot)" in caplog.text
    assert TOKEN not in caplog.text


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


def test_the_key_is_ignored_while_a_robot_is_attached():
    class Device:
        def start(self):
            pass

        def stop(self):
            return b""

    switch = DeviceSwitch(Device(), Device())
    events: asyncio.Queue[str] = asyncio.Queue()
    on_key = entry.key_handler(switch, events)
    on_key("down")
    switch.attach(object())
    on_key("up")  # the robot owns the button now
    assert events.get_nowait() == "down"
    assert events.empty()
