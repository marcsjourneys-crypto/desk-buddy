import pytest

from desk_buddy.audio import BYTES_PER_SECOND
from desk_buddy.stt import WhisperTranscriber
from desk_buddy.tts import SapiSynthesizer

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def transcriber():
    return WhisperTranscriber("base.en", device="cpu", compute_type="int8")


def test_silence_is_no_speech(transcriber):
    assert transcriber.transcribe(b"\x00" * BYTES_PER_SECOND * 2) == ""


def test_tts_to_stt_round_trip(transcriber):
    synth = SapiSynthesizer()
    try:
        pcm = synth.synthesize("Hello buddy, what time is it?")
    finally:
        synth.close()
    text = transcriber.transcribe(pcm).lower()
    assert "hello" in text
    assert "time" in text
