"""Unit tests for the Piper voice: the resampler, PiperSynthesizer with a fake voice, and
finding or downloading a voice (download mocked)."""

import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from desk_buddy.tts import (
    OUTPUT_RATE,
    PiperSynthesizer,
    VoiceError,
    find_voice,
    resample,
    to_pcm16,
)

PIPER_RATE = 22050


def tone(freq, rate, seconds=1.0, amplitude=0.5):
    t = np.arange(int(rate * seconds)) / rate
    return amplitude * np.sin(2 * np.pi * freq * t)


def middle_rms(x):
    """RMS away from the edges, where the filter sees zero padding."""
    n = len(x) // 10
    return float(np.sqrt(np.mean(x[n:-n] ** 2)))


# --- resample ---


def test_resample_length():
    assert len(resample(np.zeros(PIPER_RATE), PIPER_RATE, OUTPUT_RATE)) == OUTPUT_RATE
    assert len(resample(np.zeros(441), PIPER_RATE, OUTPUT_RATE)) == 320
    assert len(resample(np.zeros(0), PIPER_RATE, OUTPUT_RATE)) == 0


def test_resample_keeps_a_1_khz_tone():
    out = resample(tone(1000, PIPER_RATE), PIPER_RATE, OUTPUT_RATE)
    assert middle_rms(out) == pytest.approx(middle_rms(tone(1000, OUTPUT_RATE)), rel=0.01)
    # ...and it is still a 1 kHz tone: compare against the ideal one, sample by sample.
    ideal = tone(1000, OUTPUT_RATE)
    n = len(out) // 10
    assert np.max(np.abs(out[n:-n] - ideal[n:-n])) < 0.01


def test_resample_removes_a_10_khz_tone():
    # 10 kHz is above the 8 kHz output Nyquist: without a low-pass it would alias to 6 kHz.
    out = resample(tone(10_000, PIPER_RATE), PIPER_RATE, OUTPUT_RATE)
    assert middle_rms(out) < 0.01 * middle_rms(tone(10_000, PIPER_RATE))  # below -40 dB


def test_resample_at_the_same_rate_is_a_copy():
    x = tone(440, OUTPUT_RATE, 0.1)
    assert np.array_equal(resample(x, OUTPUT_RATE, OUTPUT_RATE), x)


def test_to_pcm16_scales_and_saturates():
    pcm = to_pcm16(np.array([0.0, 0.5, -0.5, 1.0, -1.0, 3.0, -3.0]))
    assert np.frombuffer(pcm, dtype="<i2").tolist() == [
        0,
        16384,
        -16384,
        32767,
        -32767,
        32767,
        -32768,
    ]


# --- PiperSynthesizer ---


class FakePiperVoice:
    """Yields two chunks of a 1 kHz tone at 22050 Hz and records the configs it got."""

    def __init__(self):
        self.configs, self.texts = [], []
        self.active = 0
        self.overlapped = False

    def synthesize(self, text, syn_config=None):
        self.active += 1
        self.overlapped |= self.active > 1
        time.sleep(0.01)
        self.texts.append(text)
        self.configs.append(syn_config)
        audio = tone(1000, PIPER_RATE, 0.5)
        yield SimpleNamespace(sample_rate=PIPER_RATE, audio_float_array=audio[:5000])
        yield SimpleNamespace(sample_rate=PIPER_RATE, audio_float_array=audio[5000:])
        self.active -= 1


def make_synth(speed=1.0):
    voice = FakePiperVoice()
    loaded = []
    synth = PiperSynthesizer(
        "v.onnx", speed=speed, loader=lambda path: loaded.append(path) or voice
    )
    return synth, voice, loaded


def test_piper_returns_16_khz_int16_of_the_right_length():
    synth, voice, loaded = make_synth()
    pcm = synth.synthesize("Hello there.")
    assert loaded == ["v.onnx"]
    assert voice.texts == ["Hello there."]
    assert len(pcm) == 2 * (len(tone(1000, PIPER_RATE, 0.5)) * OUTPUT_RATE // PIPER_RATE)
    samples = np.frombuffer(pcm, dtype="<i2")
    assert 0.4 * 32767 < np.max(np.abs(samples)) <= 0.51 * 32767


@pytest.mark.parametrize(("speed", "length_scale"), [(1.0, 1.0), (1.25, 0.8), (0.5, 2.0)])
def test_speed_becomes_length_scale(speed, length_scale):
    synth, voice, _ = make_synth(speed)
    synth.synthesize("Hi.")
    assert voice.configs[0].length_scale == pytest.approx(length_scale)


@pytest.mark.parametrize("text", ["", "   ", "\n\t "])
def test_piper_gives_no_audio_for_empty_text(text):
    synth, voice, _ = make_synth()
    assert synth.synthesize(text) == b""
    assert voice.texts == []


def test_piper_calls_never_overlap():
    synth, voice, _ = make_synth()
    threads = [threading.Thread(target=synth.synthesize, args=(f"t{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert len(voice.texts) == 4
    assert not voice.overlapped


def test_piper_close_is_safe_twice():
    synth, _, _ = make_synth()
    synth.close()
    synth.close()


# --- find_voice ---

NAME = "en_GB-northern_english_male-medium"


def put_voice(folder, name=NAME):
    (folder / f"{name}.onnx").write_bytes(b"model")
    (folder / f"{name}.onnx.json").write_text("{}")


def test_a_voice_already_in_the_folder_is_used_without_downloading(tmp_path):
    put_voice(tmp_path)

    def download(name, folder):
        raise AssertionError("must not download")

    assert find_voice(NAME, tmp_path, download=download) == tmp_path / f"{NAME}.onnx"


def test_a_missing_voice_is_downloaded_with_a_log_line(tmp_path, caplog):
    caplog.set_level("INFO")
    folder = tmp_path / "voices"  # doesn't exist yet
    calls = []

    def download(name, where):
        calls.append((name, where))
        put_voice(where, name)

    assert find_voice(NAME, folder, download=download) == folder / f"{NAME}.onnx"
    assert calls == [(NAME, folder)]
    assert f"Downloading voice {NAME} (about 60 MB)" in caplog.text


def test_a_failed_download_is_a_clear_error_and_leaves_no_partial_files(tmp_path):
    def download(name, where):
        (where / f"{name}.onnx").write_bytes(b"half")
        raise OSError("HTTP Error 404: Not Found")

    with pytest.raises(VoiceError, match=f"Couldn't get voice '{NAME}'.*404"):
        find_voice(NAME, tmp_path, download=download)
    assert list(tmp_path.iterdir()) == []


def test_a_download_that_produces_nothing_is_an_error(tmp_path):
    with pytest.raises(VoiceError, match="Couldn't get voice"):
        find_voice(NAME, tmp_path, download=lambda name, where: None)


def test_a_voice_path_is_used_as_is(tmp_path):
    model = tmp_path / "custom.onnx"
    model.write_bytes(b"model")
    (tmp_path / "custom.onnx.json").write_text("{}")
    assert find_voice(str(model), tmp_path / "unused", download=None) == model


def test_a_voice_path_without_its_config_is_an_error(tmp_path):
    model = tmp_path / "custom.onnx"
    model.write_bytes(b"model")
    with pytest.raises(VoiceError, match="custom.onnx.json"):
        find_voice(str(model), tmp_path, download=None)
