"""Unit tests for the Piper voice: the resampler, PiperSynthesizer with a fake voice, and
finding or downloading a voice (urlopen mocked)."""

import io
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
    load_piper,
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


@pytest.mark.parametrize("speed", [0.4, 2.1, float("nan")])
def test_piper_refuses_a_speed_out_of_range(speed):
    with pytest.raises(ValueError, match="speed"):
        PiperSynthesizer("v.onnx", speed=speed, loader=lambda path: FakePiperVoice())


def test_piper_refuses_chunks_with_mixed_sample_rates():
    class MixedVoice(FakePiperVoice):
        def synthesize(self, text, syn_config=None):
            yield SimpleNamespace(sample_rate=PIPER_RATE, audio_float_array=np.zeros(100))
            yield SimpleNamespace(sample_rate=16_000, audio_float_array=np.zeros(100))

    synth = PiperSynthesizer("v.onnx", loader=lambda path: MixedVoice())
    with pytest.raises(ValueError, match="sample rate"):
        synth.synthesize("Hi.")


# --- find_voice ---

NAME = "en_GB-northern_english_male-medium"


def put_voice(folder, name=NAME):
    (folder / f"{name}.onnx").write_bytes(b"model")
    (folder / f"{name}.onnx.json").write_text("{}")


class FakeWeb:
    """Stands in for urllib's urlopen: serves the two voice files, or fails on one."""

    def __init__(self, fail_on=None):
        self.fail_on, self.calls = fail_on, []

    def __call__(self, url, timeout=None):
        self.calls.append((url, timeout))
        if self.fail_on and url.split("?")[0].endswith(self.fail_on):
            raise OSError("HTTP Error 404: Not Found")
        return io.BytesIO(b"{}" if ".onnx.json" in url else b"model-bytes")


def never_download(url, timeout=None):
    raise AssertionError("must not download")


def test_a_voice_already_in_the_folder_is_used_without_downloading(tmp_path):
    put_voice(tmp_path)
    assert find_voice(NAME, tmp_path, opener=never_download) == tmp_path / f"{NAME}.onnx"


def test_a_missing_voice_is_downloaded_with_a_timeout_and_a_log_line(tmp_path, caplog):
    caplog.set_level("INFO")
    folder = tmp_path / "voices"  # doesn't exist yet
    web = FakeWeb()

    assert find_voice(NAME, folder, opener=web) == folder / f"{NAME}.onnx"

    assert (folder / f"{NAME}.onnx").read_bytes() == b"model-bytes"
    assert (folder / f"{NAME}.onnx.json").read_text() == "{}"
    assert sorted(p.name for p in folder.iterdir()) == [f"{NAME}.onnx", f"{NAME}.onnx.json"]
    urls = [url.split("?")[0] for url, _ in web.calls]
    assert urls == [
        f"https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_GB/"
        f"northern_english_male/medium/{NAME}{ext}"
        for ext in (".onnx", ".onnx.json")
    ]
    assert all(timeout == 30 for _, timeout in web.calls)
    assert f"Downloading voice {NAME} (about 60 MB)" in caplog.text


def test_only_medium_voices_mention_a_size(tmp_path, caplog):
    caplog.set_level("INFO")
    find_voice("en_US-lessac-high", tmp_path, opener=FakeWeb())
    assert "Downloading voice en_US-lessac-high…" in caplog.text
    assert "MB" not in caplog.text


@pytest.mark.parametrize("fail_on", [".onnx", ".onnx.json"])
def test_a_failed_download_is_a_clear_error_and_leaves_nothing_behind(tmp_path, fail_on):
    with pytest.raises(VoiceError, match=f"Couldn't get voice '{NAME}'.*404"):
        find_voice(NAME, tmp_path, opener=FakeWeb(fail_on=fail_on))
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("present", [".onnx", ".onnx.json"])
def test_a_half_present_voice_is_downloaded_again(tmp_path, present):
    (tmp_path / f"{NAME}{present}").write_bytes(b"stale")
    web = FakeWeb()
    find_voice(NAME, tmp_path, opener=web)
    assert len(web.calls) == 2
    assert (tmp_path / f"{NAME}.onnx").read_bytes() == b"model-bytes"
    assert (tmp_path / f"{NAME}.onnx.json").read_text() == "{}"


@pytest.mark.parametrize(
    "name",
    ["../../evil-x-medium", "en_GB-a/b-medium", "en_GB-a\\b-medium", "en_GB-..-medium", "x"],
)
def test_a_bad_voice_name_is_refused_before_any_download(tmp_path, name):
    with pytest.raises(VoiceError, match="not a Piper voice name"):
        find_voice(name, tmp_path, opener=never_download)
    assert list(tmp_path.iterdir()) == []


def test_a_voice_path_is_used_as_is(tmp_path):
    model = tmp_path / "custom.onnx"
    model.write_bytes(b"model")
    (tmp_path / "custom.onnx.json").write_text("{}")
    assert find_voice(str(model), tmp_path / "unused", opener=never_download) == model


def test_a_voice_path_without_its_config_is_an_error(tmp_path):
    model = tmp_path / "custom.onnx"
    model.write_bytes(b"model")
    with pytest.raises(VoiceError, match="custom.onnx.json"):
        find_voice(str(model), tmp_path, opener=never_download)


# --- load_piper ---


def broken_loader(path):
    raise RuntimeError("bad model")


def test_load_piper_downloads_and_loads(tmp_path):
    voice = FakePiperVoice()
    synth = load_piper(NAME, tmp_path, speed=1.25, opener=FakeWeb(), loader=lambda p: voice)
    synth.synthesize("Hi.")
    assert voice.configs[0].length_scale == pytest.approx(0.8)


def test_a_just_downloaded_voice_that_fails_to_load_is_deleted(tmp_path):
    with pytest.raises(VoiceError, match=f"Can't load Piper voice '{NAME}': bad model"):
        load_piper(NAME, tmp_path, opener=FakeWeb(), loader=broken_loader)
    assert list(tmp_path.iterdir()) == []  # the next run downloads it again


def test_a_voice_that_was_already_there_is_kept_if_it_fails_to_load(tmp_path):
    put_voice(tmp_path)
    with pytest.raises(VoiceError, match="bad model"):
        load_piper(NAME, tmp_path, opener=never_download, loader=broken_loader)
    assert (tmp_path / f"{NAME}.onnx").exists()


def test_a_custom_voice_file_is_never_deleted(tmp_path):
    model = tmp_path / "custom.onnx"
    model.write_bytes(b"model")
    (tmp_path / "custom.onnx.json").write_text("{}")
    with pytest.raises(VoiceError):
        load_piper(str(model), tmp_path, opener=never_download, loader=broken_loader)
    assert model.exists()
