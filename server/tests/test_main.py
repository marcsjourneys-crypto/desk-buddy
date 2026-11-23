import desk_buddy.__main__ as entry
from desk_buddy.audio import AudioDeviceError
from desk_buddy.config import Settings


class FakeTranscriber:
    def __init__(self, *args, **kwargs):
        self.closed = False

    def close(self):
        self.closed = True


class FakeSynthesizer:
    instances: list["FakeSynthesizer"] = []

    def __init__(self):
        self.closed = False
        FakeSynthesizer.instances.append(self)

    def close(self):
        self.closed = True


def failing_microphone(*args, **kwargs):
    raise AudioDeviceError("no mic")


def test_startup_failure_prints_reason_and_closes_what_was_built(monkeypatch, capsys):
    FakeSynthesizer.instances = []
    monkeypatch.setattr(entry, "load_settings", lambda: Settings(anthropic_api_key="k"))
    monkeypatch.setattr(entry, "check_audio_devices", lambda: None)
    monkeypatch.setattr(entry, "WhisperTranscriber", FakeTranscriber)
    monkeypatch.setattr(entry, "SapiSynthesizer", FakeSynthesizer)
    monkeypatch.setattr(entry, "Microphone", failing_microphone)

    assert entry.main() == 2

    err = capsys.readouterr().err
    assert "Can't start" in err
    assert "no mic" in err
    assert [s.closed for s in FakeSynthesizer.instances] == [True]
