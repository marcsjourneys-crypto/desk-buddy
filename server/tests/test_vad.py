"""The streaming end-of-speech detector, with the real Silero model faster-whisper ships.

tests/fixtures/speech.wav is CARL's own Piper voice (en_GB-northern_english_male-medium,
16 kHz mono) saying "Right. What's the weather going to be like in Leeds tomorrow
afternoon?" (3.70 s, a short pause after "Right."). It was made once with
PiperSynthesizer.synthesize and wave, so these tests need no voice and no network.
"""

import threading
import wave
from pathlib import Path

import numpy as np
import pytest

from desk_buddy import vad
from desk_buddy.audio import SAMPLE_RATE
from desk_buddy.vad import CHUNK_SAMPLES, Endpointer, Event

FIXTURE = Path(__file__).parent / "fixtures" / "speech.wav"
FRAME = 640  # 20 ms, as the robot sends it
CHUNK_SECONDS = CHUNK_SAMPLES / SAMPLE_RATE


def speech() -> bytes:
    with wave.open(str(FIXTURE), "rb") as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (SAMPLE_RATE, 1, 2)
        return w.readframes(w.getnframes())


def silence(seconds: float) -> bytes:
    return bytes(round(seconds * SAMPLE_RATE) * 2)


def to_pcm(x: np.ndarray) -> bytes:
    return (np.clip(x, -1, 1) * 32767).astype("<i2").tobytes()


def run(endpointer: Endpointer, pcm: bytes, frame: int = FRAME) -> list[tuple[Event, float]]:
    """Feed `pcm` in frames; every event with the audio time (in s) it happened at."""
    events = []
    for i in range(0, len(pcm), frame):
        if (event := endpointer.feed(pcm[i : i + frame])) is not None:
            events.append((event, endpointer.samples / SAMPLE_RATE))
    return events


def last_voiced_seconds(pcm: bytes) -> float:
    """Where the fixture's speech ends, by loudness (the end of the last loud chunk)."""
    x = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768
    loud = [
        i
        for i in range(0, len(x) - CHUNK_SAMPLES, CHUNK_SAMPLES)
        if np.sqrt(np.mean(x[i : i + CHUNK_SAMPLES] ** 2)) > 0.01
    ]
    return (loud[-1] + CHUNK_SAMPLES) / SAMPLE_RATE


# --- the real model ---


def test_silence_is_no_speech_at_four_seconds():
    assert run(Endpointer(), silence(6)) == [(Event.NO_SPEECH, 4.0)]


T = np.arange(SAMPLE_RATE * 6) / SAMPLE_RATE
RNG = np.random.default_rng(42)


@pytest.mark.parametrize(
    "name",
    [
        "white noise, quiet",
        "white noise, loud",
        "white noise, very loud",
        "tone 200 Hz",
        "tone 440 Hz",
        "tone 1 kHz",
        "tone 3 kHz",
        "tone bursts",
    ],
)
def test_noise_and_tones_are_not_speech(name):
    """Measured: white noise peaks at p = 0.21, a 200 Hz tone at 0.42, others below 0.11."""
    signal = {
        "white noise, quiet": RNG.normal(0, 0.01, T.size),
        "white noise, loud": RNG.normal(0, 0.1, T.size),
        "white noise, very loud": RNG.normal(0, 0.5, T.size),
        "tone 200 Hz": 0.3 * np.sin(2 * np.pi * 200 * T),
        "tone 440 Hz": 0.3 * np.sin(2 * np.pi * 440 * T),
        "tone 1 kHz": 0.3 * np.sin(2 * np.pi * 1000 * T),
        "tone 3 kHz": 0.3 * np.sin(2 * np.pi * 3000 * T),
        "tone bursts": np.where(T % 0.5 < 0.2, 0.3 * np.sin(2 * np.pi * 440 * T), 0),
    }[name]
    endpointer = Endpointer()
    assert run(endpointer, to_pcm(signal)) == [(Event.NO_SPEECH, 4.0)]
    assert not endpointer.heard_speech


def test_speech_starts_then_ends_after_the_trailing_silence():
    pcm = speech()
    events = run(Endpointer(), pcm + silence(3))
    assert [e for e, _ in events] == [Event.SPEECH_START, Event.END]
    started, ended = events[0][1], events[1][1]
    assert started < 0.5  # measured 0.29 s
    # Measured 4.26 s: the speech ends at 3.46 s, plus 0.8 s. The pause after "Right."
    # doesn't end it, and the end comes within a few chunks of end_silence.
    expected = last_voiced_seconds(pcm) + 0.8
    assert expected - 2 * CHUNK_SECONDS <= ended <= expected + 3 * CHUNK_SECONDS


def test_a_short_end_silence_ends_at_the_pause_after_the_first_word():
    """The pause after "Right." is between 0.4 and 0.8 s long: 0.4 s ends the question
    there (measured 1.06 s), the default 0.8 s doesn't."""
    pcm = speech() + silence(3)
    assert dict(run(Endpointer(end_silence=0.4), pcm))[Event.END] < 1.5
    assert dict(run(Endpointer(), pcm))[Event.END] > 3.7


def test_speech_after_a_pause_is_still_heard():
    events = run(Endpointer(), silence(1.5) + speech() + silence(3))
    assert [e for e, _ in events] == [Event.SPEECH_START, Event.END]
    assert 1.5 < events[0][1] < 2.0


def test_speech_that_starts_too_late_is_no_speech():
    assert run(Endpointer(), silence(4.5) + speech()) == [(Event.NO_SPEECH, 4.0)]


def test_continuous_speech_is_cut_at_the_cap():
    looped = speech() * 6  # 22 s, with no pause as long as 0.8 s
    events = run(Endpointer(), looped)
    assert [e for e, _ in events] == [Event.SPEECH_START, Event.MAX]
    # The first whole chunk at or after 15 s (469 chunks of 32 ms).
    assert events[1][1] == pytest.approx(15.008)


def test_after_the_end_it_is_done_until_reset():
    endpointer = Endpointer()
    first = run(endpointer, speech() + silence(3))
    assert endpointer.done is Event.END
    assert run(endpointer, speech() + silence(3)) == []
    endpointer.reset()  # the model's state too, so the result is the same again
    assert run(endpointer, speech() + silence(3)) == first


@pytest.mark.parametrize("frame", [1, 333, 1024, 10**7])
def test_any_frame_size_gives_the_same_end(frame):
    pcm = speech() + silence(3)
    expected = run(Endpointer(), pcm)
    events = run(Endpointer(), pcm, frame=frame)
    if frame == 10**7:  # one call: the terminal event wins, heard_speech says it started
        assert events == [expected[-1]]
    else:
        assert events == expected


def test_load_model_loads_once_across_threads():
    sessions = []
    threads = [threading.Thread(target=lambda: sessions.append(vad.load_model())) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(sessions) == 4 and all(s is sessions[0] for s in sessions)


# --- the logic, with scripted probabilities ---


class ScriptedModel:
    """Returns the given probabilities, one per chunk, and checks what it is fed."""

    def __init__(self, probabilities):
        self.probabilities = list(probabilities)
        self.inputs = []

    def run(self, _outputs, feeds):
        assert feeds["input"].shape == (1, 576) and feeds["input"].dtype == np.float32
        assert feeds["h"].shape == feeds["c"].shape == (1, 1, 128)
        self.inputs.append(feeds["input"].copy())
        p = self.probabilities.pop(0)
        return np.array([p], dtype=np.float32), feeds["h"] + 1, feeds["c"] + 1


CHUNK = bytes(CHUNK_SAMPLES * 2)


def feed_chunks(endpointer, n):
    return [endpointer.feed(CHUNK) for _ in range(n)]


def test_hysteresis_like_silero():
    # Speech starts at >= 0.5 only, and a pause starts below 0.35 only.
    model = ScriptedModel([0.49, 0.5, 0.4, 0.4, 0.34])
    endpointer = Endpointer(model=model)
    assert endpointer.feed(CHUNK) is None
    assert not endpointer.heard_speech
    assert endpointer.feed(CHUNK) is Event.SPEECH_START
    assert feed_chunks(endpointer, 2) == [None, None]
    assert endpointer.silence_from is None  # 0.4 is between the thresholds
    assert endpointer.feed(CHUNK) is None
    assert endpointer.silence_from == 4 * CHUNK_SAMPLES  # the pause began at chunk 4


def test_a_pause_ends_only_after_end_silence_below_the_lower_threshold():
    model = ScriptedModel([0.9] + [0.2] * 24 + [0.6] + [0.2] * 25)
    endpointer = Endpointer(model=model)
    assert endpointer.feed(CHUNK) is Event.SPEECH_START
    assert set(feed_chunks(endpointer, 24)) == {None}  # 24 quiet chunks: not yet
    assert endpointer.feed(CHUNK) is None  # speech again: the pause is over
    assert endpointer.silence_from is None
    events = feed_chunks(endpointer, 25)
    assert events[:-1] == [None] * 24 and events[-1] is Event.END
    assert endpointer.samples == 51 * CHUNK_SAMPLES


def test_probabilities_between_the_thresholds_neither_start_nor_end_a_pause():
    model = ScriptedModel([0.9, 0.2] + [0.4] * 30 + [0.2] * 30)
    endpointer = Endpointer(model=model, no_speech_timeout=100, max_seconds=100)
    feed_chunks(endpointer, 2)
    assert endpointer.silence_from == CHUNK_SAMPLES
    # In-between chunks don't reset the pause either, so it ends 25 chunks after it began.
    events = feed_chunks(endpointer, 30)
    assert events.index(Event.END) == 23  # chunk 26 overall: 25 chunks after chunk 1


def test_the_model_gets_float_audio_with_the_last_64_samples_as_context():
    model = ScriptedModel([0.0, 0.0])
    endpointer = Endpointer(model=model)
    ramp = np.arange(2 * CHUNK_SAMPLES, dtype="<i2")
    endpointer.feed(ramp.tobytes())
    first, second = model.inputs
    assert not first[0, :64].any()  # no context before the first chunk
    assert first[0, 64:] == pytest.approx(ramp[:512] / 32768)
    assert second[0, :64] == pytest.approx(ramp[448:512] / 32768)
    assert second[0, 64:] == pytest.approx(ramp[512:] / 32768)


def test_a_partial_chunk_waits_for_more_audio():
    model = ScriptedModel([0.9])
    endpointer = Endpointer(model=model)
    assert endpointer.feed(bytes(1023)) is None
    assert model.inputs == []
    assert endpointer.feed(bytes(1)) is Event.SPEECH_START


def test_a_bad_threshold_is_refused():
    with pytest.raises(ValueError):
        Endpointer(threshold=0)
