import pytest

from desk_buddy.audio import BYTES_PER_SECOND, duration_seconds
from desk_buddy.tts import SapiSynthesizer

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def synth():
    s = SapiSynthesizer()
    yield s
    s.close()


def test_output_format_is_16k_16bit_mono(synth):
    assert synth.audio_format() == (16_000, 16, 1)


def test_synthesizes_speech(synth):
    pcm = synth.synthesize("Hello, I am Buddy.")
    assert len(pcm) % 2 == 0
    assert 0.3 < duration_seconds(pcm) < 5
    assert len(pcm) > BYTES_PER_SECOND // 4


def test_markup_is_spoken_as_text_not_xml(synth):
    # With SVSFIsNotXML, '<' is read as text instead of being parsed as SAPI XML.
    assert len(synth.synthesize("<volume level='0'>quiet</volume>")) > 0
