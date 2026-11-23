"""Text-to-speech. Every engine returns 16 kHz 16-bit mono PCM bytes."""

from __future__ import annotations

import gc
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Protocol

log = logging.getLogger(__name__)

SAFT_16KHZ_16BIT_MONO = 18  # SpeechAudioFormatType
SVSF_IS_NOT_XML = 16  # SpeechVoiceSpeakFlags: never parse model text as SAPI XML
CLOSE_TIMEOUT_S = 5.0


class Synthesizer(Protocol):
    def synthesize(self, text: str) -> bytes: ...


class SapiSynthesizer:
    """Windows SAPI voice via comtypes.

    COM objects belong to the thread that made them, so every SAPI call runs on one
    dedicated worker thread. synthesize() is blocking and safe to call from any thread;
    calls are serialized.

    An in-flight Speak cannot be aborted: if the caller is cancelled (e.g. an asyncio
    task awaiting it in an executor), the render still runs to completion on the SAPI
    thread and its result is simply discarded.
    """

    def __init__(self) -> None:
        self._voice: Any = None
        self._close_lock = threading.Lock()
        self._closed = False
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="sapi", initializer=_co_initialize
        )
        try:
            self._executor.submit(self._create).result()
        except BaseException:
            self._executor.shutdown(wait=True)
            raise

    def synthesize(self, text: str) -> bytes:
        if not text.strip():
            return b""
        return self._executor.submit(self._synthesize, text).result()

    def audio_format(self) -> tuple[int, int, int]:
        """(samples/s, bits/sample, channels) of the output; used by tests."""
        return self._executor.submit(self._audio_format).result()

    def close(self) -> None:
        """Release the voice and uninitialize COM on its thread. Safe to call twice."""
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        try:
            self._executor.submit(self._destroy).result(timeout=CLOSE_TIMEOUT_S)
        except Exception:
            log.exception("SAPI shutdown failed")
        self._executor.shutdown(wait=True)

    # --- runs on the SAPI thread ---

    def _create(self) -> None:
        import comtypes.client

        self._voice = comtypes.client.CreateObject("SAPI.SpVoice")

    def _destroy(self) -> None:
        import comtypes

        self._voice = None
        gc.collect()  # drop any lingering COM pointers before uninitializing
        comtypes.CoUninitialize()

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
        try:
            self._voice.Speak(text, SVSF_IS_NOT_XML)
            data = stream.GetData()  # comtypes returns a tuple of ints
        finally:
            self._voice.AudioOutputStream = None
        return bytes(data) if data else b""

    def _audio_format(self) -> tuple[int, int, int]:
        wfx = self._new_stream().Format.GetWaveFormatEx()
        return (wfx.SamplesPerSec, wfx.BitsPerSample, wfx.Channels)


def _co_initialize() -> None:
    import comtypes

    comtypes.CoInitialize()
