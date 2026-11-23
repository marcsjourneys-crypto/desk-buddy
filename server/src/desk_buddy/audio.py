"""PC mic and speaker via sounddevice. Audio everywhere is 16 kHz, 16-bit, mono PCM bytes."""

from __future__ import annotations

import contextlib
import threading

import sounddevice as sd

SAMPLE_RATE = 16_000
BYTES_PER_SECOND = SAMPLE_RATE * 2
CHUNK_BYTES = BYTES_PER_SECOND // 50  # 20 ms
FORMAT = "16 kHz, int16, 1 channel"


class AudioDeviceError(Exception):
    """No usable input or output device."""


def duration_seconds(pcm: bytes) -> float:
    return len(pcm) / BYTES_PER_SECOND


def check_audio_devices() -> None:
    """Fail fast with the device list if the default mic or speaker can't do our format."""
    try:
        sd.query_devices(kind="input")
        sd.query_devices(kind="output")
        sd.check_input_settings(samplerate=SAMPLE_RATE, channels=1, dtype="int16")
        sd.check_output_settings(samplerate=SAMPLE_RATE, channels=1, dtype="int16")
    except (ValueError, sd.PortAudioError) as exc:
        raise AudioDeviceError(f"{exc}\nAvailable devices:\n{sd.query_devices()}") from exc


def _describe_device(kind: str, device: int | str | None) -> str:
    try:
        info = sd.query_devices(device, kind=kind)
        hostapi = sd.query_hostapis(info["hostapi"])["name"]
        return f"'{info['name']}' ({hostapi})"
    except Exception:  # noqa: BLE001 - diagnostics only, never mask the real error
        return repr(device)


def _open_stream(factory, kind: str, device: int | str | None, **kwargs):
    """Create and start a stream; on failure close it and raise a descriptive error."""
    stream = None
    try:
        stream = factory(samplerate=SAMPLE_RATE, channels=1, dtype="int16", device=device, **kwargs)
        stream.start()
        return stream
    except sd.PortAudioError as exc:
        if stream is not None:
            with contextlib.suppress(Exception):
                stream.close()
        raise AudioDeviceError(
            f"Can't open {kind} device {_describe_device(kind, device)} as {FORMAT}: {exc}"
        ) from exc


class PcmBuffer:
    """Thread-safe PCM accumulator with a size cap (extra audio is dropped)."""

    def __init__(self, max_seconds: float) -> None:
        self._max = int(max_seconds * BYTES_PER_SECOND) & ~1  # whole int16 samples
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

    def __init__(self, max_seconds: float = 30.0, device: int | str | None = None) -> None:
        self._buffer = PcmBuffer(max_seconds)
        self._recording = threading.Event()
        self._closed = False
        self._stream = _open_stream(
            sd.RawInputStream,
            "input",
            device,
            blocksize=CHUNK_BYTES // 2,
            callback=self._callback,
        )

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
        if self._closed:
            return
        self._closed = True
        self._recording.clear()
        try:
            self._stream.stop()
        finally:
            self._stream.close()


class Speaker:
    """Plays PCM through one long-lived output stream, so sentences play back to back, gapless.

    Turn tokens: stop() bumps a generation counter. play() only writes while the token it
    was given is still current, so a stale play from a cancelled turn can't leak audio.
    """

    def __init__(self, device: int | str | None = None) -> None:
        self._generation = 0
        self._gen_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._closed = False
        self._stream = _open_stream(sd.RawOutputStream, "output", device, latency="low")

    def current_token(self) -> int:
        return self._generation

    def play(self, pcm: bytes, token: int) -> None:
        """Blocking. Writes nothing if `token` is stale; stops writing as soon as stop() is
        called. Up to one device buffer of already-queued audio may still play after that."""
        pcm = pcm[: len(pcm) & ~1]
        for i in range(0, len(pcm), CHUNK_BYTES):
            with self._write_lock:
                if self._closed or token != self._generation:
                    return
                self._stream.write(pcm[i : i + CHUNK_BYTES])

    def stop(self) -> None:
        with self._gen_lock:
            self._generation += 1

    def close(self) -> None:
        self.stop()
        with self._write_lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._stream.abort()
            finally:
                self._stream.close()
