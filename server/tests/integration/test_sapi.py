import threading

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
    # With SVSFIsNotXML the tags are read aloud, so the audio is clearly longer
    # (measured ~1.55x on SAPI's default voice).
    plain = synth.synthesize("quiet")
    tagged = synth.synthesize("<b>quiet</b>")
    assert len(tagged) > 1.25 * len(plain)


@pytest.mark.parametrize("text", ["", "   ", "\n\t "])
def test_empty_text_gives_no_audio(synth, text):
    assert synth.synthesize(text) == b""


def test_close_twice_does_not_raise():
    s = SapiSynthesizer()
    s.close()
    s.close()


def test_calls_from_two_threads_both_succeed(synth):
    results: dict[str, bytes] = {}
    errors: list[BaseException] = []

    def speak(name: str) -> None:
        try:
            results[name] = synth.synthesize(f"Hello from {name}.")
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=speak, args=(n,)) for n in ("one", "two")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors
    assert set(results) == {"one", "two"}
    assert all(len(pcm) > BYTES_PER_SECOND // 4 for pcm in results.values())
