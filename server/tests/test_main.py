import pytest

import desk_buddy.__main__ as entry
from desk_buddy.audio import AudioDeviceError
from desk_buddy.config import Settings


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
            pass

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
    assert calls == ["transcriber created", "synthesizer created", "synthesizer closed"]


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

    assert calls == [
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
