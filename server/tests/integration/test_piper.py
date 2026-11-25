"""Real Piper synthesis with CARL's voice. Needs the voice in VOICES_DIR, or the network
to download it (about 60 MB) on the first run."""

import time

import numpy as np
import pytest

from desk_buddy.audio import duration_seconds
from desk_buddy.config import Settings
from desk_buddy.tts import PiperSynthesizer, find_voice

pytestmark = pytest.mark.integration

LINE = "Don't panic. CARL is online."


@pytest.fixture(scope="module")
def synth():
    settings = Settings(anthropic_api_key="unused")
    s = PiperSynthesizer(find_voice(settings.tts_voice, settings.voices_dir))
    yield s
    s.close()


def test_synthesizes_plausible_16_khz_speech(synth):
    pcm = synth.synthesize(LINE)
    assert len(pcm) % 2 == 0
    assert 0.5 < duration_seconds(pcm) < 5
    samples = np.frombuffer(pcm, dtype="<i2")
    assert np.max(np.abs(samples)) > 3000  # not silence


def test_a_typical_sentence_renders_faster_than_real_time(synth):
    synth.synthesize("Warm up.")  # the first call pays for onnxruntime's warm-up
    sentence = "Sure, I've noted that you need to pick up dog food on the way home."
    started = time.perf_counter()
    pcm = synth.synthesize(sentence)
    elapsed = time.perf_counter() - started
    print(f"\n{elapsed * 1000:.0f} ms for {duration_seconds(pcm):.2f} s of speech")
    assert elapsed < duration_seconds(pcm)  # measured ~20x faster; this only catches a hang
