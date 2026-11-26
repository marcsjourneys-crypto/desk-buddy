"""The end-of-speech detector on a fresh sentence in CARL's Piper voice. Needs the voice in
VOICES_DIR, or the network to download it (about 60 MB) on the first run. The unit tests
use a committed recording of the same voice (tests/fixtures/speech.wav) instead."""

import pytest

from desk_buddy.audio import SAMPLE_RATE
from desk_buddy.config import Settings
from desk_buddy.tts import PiperSynthesizer, find_voice
from desk_buddy.vad import Endpointer, Event

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def synth():
    settings = Settings(anthropic_api_key="unused")
    s = PiperSynthesizer(find_voice(settings.tts_voice, settings.voices_dir))
    yield s
    s.close()


def test_a_piper_sentence_starts_and_ends(synth):
    pcm = synth.synthesize("Remind me to buy milk on the way home tonight.")
    pcm += bytes(SAMPLE_RATE * 2 * 3)  # 3 s of silence
    endpointer = Endpointer()
    events = []
    for i in range(0, len(pcm), 640):
        if (event := endpointer.feed(pcm[i : i + 640])) is not None:
            events.append((event, endpointer.samples / SAMPLE_RATE))
    print(f"\n{events}")
    speech_seconds = len(pcm) / 2 / SAMPLE_RATE - 3
    assert [e for e, _ in events] == [Event.SPEECH_START, Event.END]
    assert events[0][1] < 0.6
    assert speech_seconds < events[1][1] < speech_seconds + 1.0
