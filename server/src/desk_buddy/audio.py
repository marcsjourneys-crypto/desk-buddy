"""PC mic and speaker via sounddevice. Audio everywhere is 16 kHz, 16-bit, mono PCM bytes."""

from __future__ import annotations

import threading

import sounddevice as sd

SAMPLE_RATE = 16_000
BYTES_PER_SECOND = SAMPLE_RATE * 2
CHUNK_BYTES = BYTES_PER_SECOND // 50  # 20 ms


class AudioDeviceError(Exception):
    """No usable input or output device."""


def duration_seconds(pcm: bytes) -> float:
    return len(pcm) / BYTES_PER_SECOND


def check_audio_devices() -> None:
    """Fail fast with the device list if there is no default mic or speaker."""
    try:
        sd.query_devices(kind="input")
        sd.query_devices(kind="output")
    except (ValueError, sd.PortAudioError) as exc:
        raise AudioDeviceError(f"{exc}\nAvailable devices:\n{sd.query_devices()}") from exc


class PcmBuffer:
    """Thread-safe PCM accumulator with a size cap (extra audio is dropped)."""

    def __init__(self, max_seconds: float) -> None:
        self._max = int(max_seconds * BYTES_PER_SECOND)
        self._chunks: list[bytes] = []
        self._size = 0
        self._lock = threading.Lock()

    def append(self, data: bytes) -> None:
        with self._lock:
            room = self._max - self._size
            if room <= 0:
                return
            chunk = data[:room]
            self._chunks.append(chunk)
            self._size += len(chunk)

    def take(self) -> bytes:
        with self._lock:
            out = b"".join(self._chunks)
            self._chunks, self._size = [], 0
            return out


class Microphone:
    """Keeps the input stream open (no start-up delay); only buffers between start() and stop()."""

    def __init__(self, max_seconds: float = 30.0) -> None:
        self._buffer = PcmBuffer(max_seconds)
        self._recording = threading.Event()
        self._stream = sd.RawInputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="int16",
            blocksize=CHUNK_BYTES // 2,
            callback=self._callback,
        )
        self._stream.start()

    def _callback(self, indata, frames, time_info, status) -> None:
        if self._recording.is_set():
            self._buffer.append(bytes(indata))

    def start(self) -> None:
        self._buffer.take()
        self._recording.set()

    def stop(self) -> bytes:
        self._recording.clear()
        return self._buffer.take()

    def close(self) -> None:
        self._stream.stop()
        self._stream.close()


class Speaker:
    """Plays PCM through one long-lived output stream, so sentences play back to back, gapless."""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._stream = sd.RawOutputStream(
            samplerate=SAMPLE_RATE, channels=1, dtype="int16", latency="low"
        )
        self._stream.start()

    def play(self, pcm: bytes) -> None:
        """Blocking; returns early (within ~20 ms) when stop() is called."""
        self._stop.clear()
        pcm = pcm[: len(pcm) // 2 * 2]
        for i in range(0, len(pcm), CHUNK_BYTES):
            if self._stop.is_set():
                return
            self._stream.write(pcm[i : i + CHUNK_BYTES])

    def stop(self) -> None:
        self._stop.set()

    def close(self) -> None:
        self._stop.set()
        self._stream.stop()
        self._stream.close()
