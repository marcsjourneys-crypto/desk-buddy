import pytest
import sounddevice as sd

from desk_buddy import audio
from desk_buddy.audio import (
    BYTES_PER_SECOND,
    CHUNK_BYTES,
    AudioDeviceError,
    Microphone,
    PcmBuffer,
    Speaker,
    check_audio_devices,
    duration_seconds,
)


def test_duration():
    assert duration_seconds(b"\x00" * BYTES_PER_SECOND) == 1.0


def test_buffer_collects_and_resets():
    buf = PcmBuffer(max_seconds=1)
    buf.append(b"ab")
    buf.append(b"cd")
    assert buf.take() == b"abcd"
    assert buf.take() == b""


def test_buffer_caps_size():
    buf = PcmBuffer(max_seconds=0.001)  # 32 bytes at 16 kHz int16
    buf.append(b"x" * 20)
    buf.append(b"y" * 20)
    buf.append(b"z" * 20)
    assert buf.take() == b"x" * 20 + b"y" * 12


def test_buffer_cap_is_even():
    buf = PcmBuffer(max_seconds=33 / BYTES_PER_SECOND)  # 33 bytes -> 32
    buf.append(b"x" * 40)
    assert len(buf.take()) == 32


# --- fakes for sounddevice streams (no real devices) ---


class FakeStream:
    def __init__(self, fail_start: bool = False, **kwargs):
        self.kwargs = kwargs
        self.callback = kwargs.get("callback")
        self.fail_start = fail_start
        self.calls: list[str] = []
        self.written: list[bytes] = []
        self.on_write = None

    def start(self):
        self.calls.append("start")
        if self.fail_start:
            raise sd.PortAudioError("Unanticipated host error")

    def stop(self):
        self.calls.append("stop")

    def abort(self):
        self.calls.append("abort")

    def close(self):
        self.calls.append("close")

    def write(self, data):
        self.written.append(bytes(data))
        if self.on_write:
            self.on_write(len(self.written))


@pytest.fixture
def fake_sd(monkeypatch):
    made: list[FakeStream] = []
    opts = {"fail_start": False}

    def factory(**kwargs):
        stream = FakeStream(fail_start=opts["fail_start"], **kwargs)
        made.append(stream)
        return stream

    def query_devices(device=None, kind=None):
        if kind is None and device is None:
            return "DEVICE LIST"
        return {"name": f"Fake {kind}", "hostapi": 0}

    monkeypatch.setattr(audio.sd, "RawInputStream", factory)
    monkeypatch.setattr(audio.sd, "RawOutputStream", factory)
    monkeypatch.setattr(audio.sd, "query_devices", query_devices)
    monkeypatch.setattr(audio.sd, "query_hostapis", lambda index=None: {"name": "FakeAPI"})
    return made, opts


def test_mic_buffers_only_between_start_and_stop(fake_sd):
    made, _ = fake_sd
    mic = Microphone()
    stream = made[0]
    assert stream.kwargs["samplerate"] == 16_000
    stream.callback(b"before", 3, None, None)
    mic.start()
    stream.callback(b"during", 3, None, None)
    assert mic.stop() == b"during"
    stream.callback(b"after!", 3, None, None)
    mic.start()
    assert mic.stop() == b""


def test_mic_close_is_idempotent(fake_sd):
    made, _ = fake_sd
    mic = Microphone()
    mic.close()
    mic.close()
    assert made[0].calls == ["start", "stop", "close"]


def test_play_writes_chunks_and_drops_odd_byte(fake_sd):
    made, _ = fake_sd
    spk = Speaker()
    spk.play(b"\x01" * (CHUNK_BYTES * 2 + 101), spk.current_token())
    sizes = [len(w) for w in made[0].written]
    assert sizes == [640, 640, 100]


def test_stop_during_play_halts_writes(fake_sd):
    made, _ = fake_sd
    spk = Speaker()
    made[0].on_write = lambda n: spk.stop() if n == 2 else None
    spk.play(b"\x00" * CHUNK_BYTES * 5, spk.current_token())
    assert len(made[0].written) == 2


def test_stale_token_writes_nothing_and_new_token_works(fake_sd):
    made, _ = fake_sd
    spk = Speaker()
    old = spk.current_token()
    spk.stop()
    spk.play(b"\x00" * CHUNK_BYTES, old)
    assert made[0].written == []
    spk.play(b"\x00" * CHUNK_BYTES, spk.current_token())
    assert len(made[0].written) == 1


def test_speaker_close_twice_aborts_then_closes(fake_sd):
    made, _ = fake_sd
    spk = Speaker()
    token = spk.current_token()
    spk.close()
    spk.close()
    assert made[0].calls == ["start", "abort", "close"]
    spk.play(b"\x00" * CHUNK_BYTES, token)  # after close: no-op
    assert made[0].written == []


@pytest.mark.parametrize("cls,kind", [(Microphone, "input"), (Speaker, "output")])
def test_failing_start_closes_stream_and_names_device(fake_sd, cls, kind):
    made, opts = fake_sd
    opts["fail_start"] = True
    with pytest.raises(AudioDeviceError) as info:
        cls()
    assert made[0].calls == ["start", "close"]
    msg = str(info.value)
    assert f"Fake {kind}" in msg
    assert "FakeAPI" in msg
    assert "16 kHz" in msg
    assert isinstance(info.value.__cause__, sd.PortAudioError)


def test_device_is_passed_through(fake_sd):
    made, _ = fake_sd
    Microphone(device=7)
    Speaker(device=3)
    assert [s.kwargs["device"] for s in made] == [7, 3]


def test_check_audio_devices_wraps_settings_errors(fake_sd, monkeypatch):
    def bad(**kwargs):
        raise sd.PortAudioError("Invalid sample rate")

    monkeypatch.setattr(audio.sd, "check_input_settings", bad)
    monkeypatch.setattr(audio.sd, "check_output_settings", lambda **kw: None)
    with pytest.raises(AudioDeviceError) as info:
        check_audio_devices()
    assert "Invalid sample rate" in str(info.value)
    assert "DEVICE LIST" in str(info.value)
