"""Text-to-speech. Every engine returns 16 kHz 16-bit mono PCM bytes."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Protocol

SAFT_16KHZ_16BIT_MONO = 18  # SpeechAudioFormatType
SVSF_IS_NOT_XML = 16  # SpeechVoiceSpeakFlags: never parse model text as SAPI XML


class Synthesizer(Protocol):
    def synthesize(self, text: str) -> bytes: ...


class SapiSynthesizer:
    """Windows SAPI voice via comtypes.

    COM objects belong to the thread that made them, so every SAPI call runs on one
    dedicated worker thread. synthesize() is blocking and safe to call from any thread.
    """

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="sapi", initializer=_co_initialize
        )
        self._executor.submit(self._create).result()

    def synthesize(self, text: str) -> bytes:
        return self._executor.submit(self._synthesize, text).result()

    def audio_format(self) -> tuple[int, int, int]:
        """(samples/s, bits/sample, channels) of the output; used by tests."""
        return self._executor.submit(self._audio_format).result()

    def close(self) -> None:
        self._executor.shutdown(wait=False)

    # --- runs on the SAPI thread ---

    def _create(self) -> None:
        import comtypes.client

        self._voice = comtypes.client.CreateObject("SAPI.SpVoice")

    def _new_stream(self):
        import comtypes.client

        fmt = comtypes.client.CreateObject("SAPI.SpAudioFormat")
        fmt.Type = SAFT_16KHZ_16BIT_MONO
        stream = comtypes.client.CreateObject("SAPI.SpMemoryStream")
        stream.Format = fmt
        return stream

    def _synthesize(self, text: str) -> bytes:
        stream = self._new_stream()
        self._voice.AudioOutputStream = stream
        self._voice.Speak(text, SVSF_IS_NOT_XML)
        return bytes(stream.GetData())

    def _audio_format(self) -> tuple[int, int, int]:
        wfx = self._new_stream().Format.GetWaveFormatEx()
        return (wfx.SamplesPerSec, wfx.BitsPerSample, wfx.Channels)


def _co_initialize() -> None:
    import comtypes

    comtypes.CoInitialize()
