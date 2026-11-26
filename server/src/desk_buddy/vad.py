"""Streaming end of speech: when has the user stopped talking?

Used after a wake word, where no key release ends the question. It runs the Silero VAD
model that faster-whisper already ships (`faster_whisper/assets/silero_vad_v6.onnx`) one
32 ms chunk at a time, carrying the model's LSTM state between chunks.

The model takes 512 new samples (32 ms at 16 kHz) plus the last 64 samples of the chunk
before as context, as float32 in [-1, 1], and its state as `h` and `c` (each 1x1x128). It
returns the speech probability of the chunk. Speech starts when the probability reaches
`threshold` and pauses when it drops below `threshold - 0.15`, the same hysteresis as
Silero's own (and faster-whisper's) `get_speech_timestamps`. Speech only counts once
there is `min_speech` (0.25 s) of it without a pause of `end_silence`, so a click or a
cough after a false wake is NO_SPEECH, never a tiny question for Whisper to fill in.

Cost: one ONNX call per 32 ms of audio, about 0.2 ms on the i7-1355U, so feed() is cheap
enough to call on the event loop for each 20 ms frame. Only the first use loads the model
(about 2 MB, tens of milliseconds): call load_model() from a worker thread at startup so
that cost never lands on the event loop. Time is counted in samples, never by the clock.
"""

from __future__ import annotations

import enum
import threading
from typing import Any

import numpy as np

from desk_buddy.audio import SAMPLE_RATE

CHUNK_SAMPLES = 512  # what the model takes at 16 kHz (32 ms)
CONTEXT_SAMPLES = 64  # the end of the chunk before, prepended to each chunk
HYSTERESIS = 0.15  # speech pauses below threshold - HYSTERESIS
_STATE_SHAPE = (1, 1, 128)

_lock = threading.Lock()
_session: Any = None


def load_model() -> Any:
    """The model's onnxruntime session, loaded once per process (thread-safe). It is the
    same session faster-whisper's own VAD filter uses, with its options (one thread)."""
    global _session
    with _lock:
        if _session is None:
            from faster_whisper.vad import get_vad_model

            _session = get_vad_model().session
        return _session


class Event(enum.Enum):
    SPEECH_START = "speech_start"  # the first speech
    END = "end"  # speech, then end_silence of non-speech
    NO_SPEECH = "no_speech"  # no speech within no_speech_timeout
    MAX = "max"  # max_seconds of audio, speech or not


TERMINAL = (Event.END, Event.NO_SPEECH, Event.MAX)


class Endpointer:
    """Feed it 16 kHz 16-bit mono PCM (any frame size); it says when the question ends.

    After a terminal event (END, NO_SPEECH or MAX) it is done: feed() returns None until
    reset(). Not thread-safe: use one instance from one thread.
    """

    def __init__(
        self,
        end_silence: float = 0.8,
        no_speech_timeout: float = 4.0,
        max_seconds: float = 15.0,
        threshold: float = 0.5,
        min_speech: float = 0.25,
        *,
        ignore_first: float = 0.0,
        model: Any = None,
    ) -> None:
        if not 0 < threshold <= 1:
            raise ValueError("threshold must be in (0, 1]")
        for name, value in (
            ("end_silence", end_silence),
            ("no_speech_timeout", no_speech_timeout),
            ("max_seconds", max_seconds),
            ("min_speech", min_speech),
        ):
            if not value > 0:  # NaN too
                raise ValueError(f"{name} must be more than 0")
        if not ignore_first >= 0:
            raise ValueError("ignore_first must be 0 or more")
        # After a wake word the robot sends ~0.3 s from before the detection, which holds
        # the end of "CARL": speech there never counts (the model still hears it).
        self._ignore_first = round(ignore_first * SAMPLE_RATE)
        self._min_speech = round(min_speech * SAMPLE_RATE)
        self._end_silence = round(end_silence * SAMPLE_RATE)
        self._no_speech = round(no_speech_timeout * SAMPLE_RATE)
        self._max = round(max_seconds * SAMPLE_RATE)
        self._threshold = threshold
        self._neg_threshold = max(threshold - HYSTERESIS, 0.01)
        self._model = model  # a session-like object with run(); loaded on first use if None
        self.reset()

    def reset(self) -> None:
        """Start over for a new question (the model's state too)."""
        self._pending = bytearray()  # PCM bytes not yet in a whole chunk
        self._context = np.zeros(CONTEXT_SAMPLES, dtype=np.float32)
        self._h = np.zeros(_STATE_SHAPE, dtype=np.float32)
        self._c = np.zeros(_STATE_SHAPE, dtype=np.float32)
        self.samples = 0  # samples judged so far (whole chunks)
        self.speaking = False  # in speech now (with hysteresis), maybe not yet real speech
        self.speech_samples = 0  # speech in the current run, until it counts as real
        self.heard_speech = False  # real speech (at least min_speech of it) since reset()
        self.silence_from: int | None = None  # where the current pause began, in samples
        self.done: Event | None = None  # the terminal event, once there is one
        self.last_probability = 0.0

    def feed(self, pcm: bytes) -> Event | None:
        """Judge every whole 32 ms chunk now buffered. Returns the event it caused, or
        None. If one call causes more than one (a big buffer), a terminal event wins over
        SPEECH_START; `heard_speech` still tells whether speech started.

        An error from onnxruntime (or the model failing to load) escapes from here: the
        caller must catch it and end the recording."""
        if self.done is not None:
            return None
        self._pending += pcm
        chunk_bytes = CHUNK_SAMPLES * 2
        result: Event | None = None
        while len(self._pending) >= chunk_bytes:
            chunk = np.frombuffer(bytes(self._pending[:chunk_bytes]), dtype="<i2")
            del self._pending[:chunk_bytes]
            event = self._judge(chunk.astype(np.float32) / 32768.0)
            if event is not None:
                result = event
            if self.done is not None:
                self._pending.clear()
                break
        return result

    def _judge(self, chunk: np.ndarray) -> Event | None:
        p = self._probability(chunk)
        self.last_probability = p
        start = self.samples
        self.samples += CHUNK_SAMPLES
        event: Event | None = None
        if start < self._ignore_first:
            p = 0.0
        if p >= self._threshold:
            self.silence_from = None
            self.speaking = True
            if not self.heard_speech:
                # A click or a cough is not a question: speech counts only once there is
                # min_speech of it, without a pause of end_silence in between.
                self.speech_samples += CHUNK_SAMPLES
                if self.speech_samples >= self._min_speech:
                    self.heard_speech = True
                    event = Event.SPEECH_START
        elif p < self._neg_threshold and self.speaking and self.silence_from is None:
            self.silence_from = start
        paused = (
            self.speaking
            and self.silence_from is not None
            and self.samples - self.silence_from >= self._end_silence
        )
        if paused and self.heard_speech:
            self.speaking = False
            event = Event.END
        else:
            if paused:  # too little speech before the pause: forget it
                self.speaking = False
                self.silence_from = None
                self.speech_samples = 0
            # A possible start that is still being judged delays NO_SPEECH by at most
            # end_silence, so someone who began just before the timeout isn't cut off.
            late = self.samples >= self._no_speech + self._end_silence
            if (
                not self.heard_speech
                and self.samples >= self._no_speech
                and (not self.speaking or late)
            ):
                event = Event.NO_SPEECH
            elif self.samples >= self._max:
                event = Event.MAX
        if event in TERMINAL:
            self.done = event
        return event

    def _probability(self, chunk: np.ndarray) -> float:
        model = self._model if self._model is not None else load_model()
        x = np.concatenate([self._context, chunk])[np.newaxis, :]
        self._context = chunk[-CONTEXT_SAMPLES:].copy()
        out, self._h, self._c = model.run(None, {"input": x, "h": self._h, "c": self._c})
        return float(np.asarray(out).reshape(-1)[0])
