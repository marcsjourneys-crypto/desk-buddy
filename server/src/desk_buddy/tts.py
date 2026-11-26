"""Text-to-speech. Every engine returns 16 kHz 16-bit mono PCM bytes."""

from __future__ import annotations

import functools
import gc
import logging
import math
import os
import shutil
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Protocol
from urllib.request import urlopen

import numpy as np

from desk_buddy.config import MAX_TTS_SPEED, MIN_TTS_SPEED, PIPER_VOICE_NAME, Settings

log = logging.getLogger(__name__)

DEFAULT_VOICE = Settings.tts_voice
DOWNLOAD_TIMEOUT_S = 30  # per request, so a stalled download fails instead of hanging

SAFT_16KHZ_16BIT_MONO = 18  # SpeechAudioFormatType
SVSF_IS_NOT_XML = 16  # SpeechVoiceSpeakFlags: never parse model text as SAPI XML
CLOSE_TIMEOUT_S = 5.0

OUTPUT_RATE = 16_000  # what the loop, the PC speaker and the robot all expect
# Silence after each sentence. Piper's library returns one chunk per sentence with no gap
# (its command-line tool adds 0.2 s by default), so lists such as headlines ran together.
SENTENCE_PAUSE_S = 0.25
CUTOFF_RATIO = 0.4625  # low-pass at 92.5 % of the lower Nyquist: 7.4 kHz for 16 kHz out
KERNEL_HALF_WIDTH = 48  # input samples each side of an output sample (96 taps)
_RESAMPLE_BLOCK = 4096  # output samples per vectorised step (bounds memory)


class Synthesizer(Protocol):
    def synthesize(self, text: str) -> bytes: ...


class VoiceError(Exception):
    """A Piper voice couldn't be found or downloaded. The message says which and why."""


def resample(x: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Resample float audio with a windowed-sinc (Blackman) low-pass interpolator.

    Each output sample is a weighted sum of the input samples around its exact position,
    with weights from a sinc low-pass at CUTOFF_RATIO of the lower Nyquist, so content
    above the new Nyquist is removed instead of aliasing. Weights are normalised per
    output sample, so DC passes exactly. len(out) == len(x) * dst_rate // src_rate.
    """
    x = np.asarray(x, dtype=np.float64)
    if src_rate == dst_rate:
        return x.copy()
    n_out = len(x) * dst_rate // src_rate
    out = np.empty(n_out)
    if n_out == 0:
        return out
    half = KERNEL_HALF_WIDTH
    table = _kernel_table(src_rate, dst_rate)  # one row of weights per phase
    phases = len(table)
    padded = np.concatenate([np.zeros(half), x, np.zeros(half + 1)])
    windows = np.lib.stride_tricks.sliding_window_view(padded, 2 * half)  # a view, no copy
    for start in range(0, n_out, _RESAMPLE_BLOCK):
        n = np.arange(start, min(start + _RESAMPLE_BLOCK, n_out), dtype=np.int64)
        base = n * src_rate // dst_rate  # the input sample at or before each output
        # windows[base + 1] covers inputs base-half+1 .. base+half (padding shifts by half).
        out[start : start + len(n)] = np.einsum("ij,ij->i", windows[base + 1], table[n % phases])
    return out


@functools.lru_cache(maxsize=8)
def _kernel_table(src_rate: int, dst_rate: int) -> np.ndarray:
    """Normalised windowed-sinc weights for each output phase: the output sample n sits a
    fraction (n * src % dst) / dst past an input sample, and that repeats every
    dst / gcd outputs (320 for 22050 -> 16000). Computed once per pair of rates."""
    half = KERNEL_HALF_WIDTH
    cutoff = CUTOFF_RATIO * min(src_rate, dst_rate) / src_rate  # cycles per input sample
    phases = dst_rate // math.gcd(src_rate, dst_rate)
    frac = (np.arange(phases, dtype=np.int64) * src_rate % dst_rate) / dst_rate
    offsets = np.arange(-half + 1, half + 1)
    d = frac[:, None] - offsets[None, :]  # distance to each input sample, in (-half, half)
    window = 0.42 + 0.5 * np.cos(np.pi * d / half) + 0.08 * np.cos(2 * np.pi * d / half)
    weights = np.sinc(2 * cutoff * d) * window
    weights /= weights.sum(axis=1, keepdims=True)
    weights.setflags(write=False)
    return weights


def to_pcm16(x: np.ndarray) -> bytes:
    """Float audio in [-1, 1] to little-endian int16 bytes, saturating outside it."""
    scaled = np.clip(np.round(np.asarray(x, dtype=np.float64) * 32767), -32768, 32767)
    return scaled.astype("<i2").tobytes()


class PiperSynthesizer:
    """A local Piper neural voice (onnxruntime on the CPU), resampled to 16 kHz.

    synthesize() is blocking and safe from any thread; a lock serialises calls so two
    renders never overlap (the loop runs it in a worker thread, like SAPI).
    """

    def __init__(
        self,
        model_path: str | Path,
        *,
        speed: float = 1.0,
        loader: Callable[[Any], Any] | None = None,
    ) -> None:
        self._length_scale = _length_scale(speed)
        if loader is None:
            from piper import PiperVoice

            loader = PiperVoice.load
        self._voice = loader(model_path)
        self._lock = threading.Lock()

    def set_speed(self, speed: float) -> None:
        """Change the speed (TTS_SPEED from the dashboard); the next sentence uses it."""
        self._length_scale = _length_scale(speed)

    def synthesize(self, text: str) -> bytes:
        if not text.strip():
            return b""
        from piper import SynthesisConfig

        config = SynthesisConfig(length_scale=self._length_scale)
        with self._lock:
            chunks = list(self._voice.synthesize(text, syn_config=config))
        if not chunks:
            return b""
        rates = {chunk.sample_rate for chunk in chunks}
        if len(rates) != 1:
            raise ValueError(f"Piper returned chunks with mixed sample rates: {sorted(rates)}")
        rate = rates.pop()
        pause = np.zeros(round(SENTENCE_PAUSE_S * rate), dtype=np.float32)
        parts = []
        for chunk in chunks:  # one per sentence, each followed by a short pause
            parts += [chunk.audio_float_array, pause]
        return to_pcm16(resample(np.concatenate(parts), rate, OUTPUT_RATE))

    def close(self) -> None:
        """Nothing to release (the model is freed with the object); safe to call twice."""


def _length_scale(speed: float) -> float:
    """Piper's length_scale for a speed: bigger is slower."""
    if not MIN_TTS_SPEED <= speed <= MAX_TTS_SPEED:  # also rejects nan
        raise ValueError(f"speed must be {MIN_TTS_SPEED}-{MAX_TTS_SPEED}, got {speed!r}")
    return 1.0 / speed


Opener = Callable[..., Any]  # urllib.request.urlopen(url, timeout=...)


def find_voice(voice: str, voices_dir: str | Path, opener: Opener = urlopen) -> Path:
    """The .onnx model for TTS_VOICE (blocking). A path is used as is. A name is looked
    up in voices_dir and downloaded there with its .onnx.json if either is missing."""
    return _find_voice(voice, voices_dir, opener)[0]


def load_piper(
    voice: str,
    voices_dir: str | Path,
    *,
    speed: float = 1.0,
    opener: Opener = urlopen,
    loader: Callable[[Any], Any] | None = None,
) -> PiperSynthesizer:
    """Find (or download) the voice and load it. Raises VoiceError. A voice downloaded
    just now that fails to load is deleted, so the next run downloads it again."""
    model, downloaded = _find_voice(voice, voices_dir, opener)
    try:
        return PiperSynthesizer(model, speed=speed, loader=loader)
    except Exception as exc:
        if downloaded:
            for path in _voice_files(model):
                path.unlink(missing_ok=True)
        raise VoiceError(f"Can't load Piper voice {voice!r}: {exc}") from exc


def _voice_files(model: Path) -> tuple[Path, Path]:
    return model, model.with_name(model.name + ".json")


def _find_voice(voice: str, voices_dir: str | Path, opener: Opener) -> tuple[Path, bool]:
    """(model path, whether it was downloaded just now)."""
    if voice.lower().endswith(".onnx"):
        model, config = _voice_files(Path(voice))
        if not (model.is_file() and config.is_file()):
            raise VoiceError(f"Voice files missing: {model} needs {config.name} next to it")
        return model, False
    if not PIPER_VOICE_NAME.fullmatch(voice):
        raise VoiceError(f"{voice!r} is not a Piper voice name (like {DEFAULT_VOICE})")
    folder = Path(voices_dir)
    model, config = _voice_files(folder / f"{voice}.onnx")
    if model.is_file() and config.is_file():
        return model, False
    size = " (about 60 MB)" if voice.endswith("-medium") else ""
    log.info("Downloading voice %s%s…", voice, size)
    try:
        _download_voice(voice, folder, opener)
    except Exception as exc:
        raise VoiceError(f"Couldn't get voice {voice!r}: {exc}") from exc
    return model, True


def _download_voice(voice: str, folder: Path, opener: Opener) -> None:
    """Download both files into a temporary subfolder, then move them into place only
    once both arrived. A half-present voice (one of the two files) is replaced."""
    from piper.download_voices import URL_FORMAT, VOICE_PATTERN

    parts = VOICE_PATTERN.match(voice)
    if parts is None:
        raise VoiceError(f"{voice!r} is not a Piper voice name")
    lang_family = parts.group("lang_family")
    url_args = {
        "lang_family": lang_family,
        "lang_code": f"{lang_family}_{parts.group('lang_region')}",
        "voice_name": parts.group("voice_name"),
        "voice_quality": parts.group("voice_quality"),
    }
    temp = folder / f".download-{voice}"
    shutil.rmtree(temp, ignore_errors=True)  # leftovers from an interrupted run
    temp.mkdir(parents=True)
    try:
        for extension in (".onnx", ".onnx.json"):
            url = URL_FORMAT.format(extension=extension, **url_args)
            with opener(url, timeout=DOWNLOAD_TIMEOUT_S) as response:
                with open(temp / f"{voice}{extension}", "wb") as out:
                    shutil.copyfileobj(response, out)
        for extension in (".onnx.json", ".onnx"):
            os.replace(temp / f"{voice}{extension}", folder / f"{voice}{extension}")
    finally:
        shutil.rmtree(temp, ignore_errors=True)


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
