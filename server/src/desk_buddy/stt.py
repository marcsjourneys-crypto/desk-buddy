"""Speech-to-text: Silero VAD gate + trim, then faster-whisper."""

from __future__ import annotations

from typing import Protocol

import numpy as np


class Transcriber(Protocol):
    def transcribe(self, pcm: bytes) -> str:
        """Return the spoken text, or '' if the clip has no speech."""
        ...


def pcm_to_float32(pcm: bytes) -> np.ndarray:
    return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0


class WhisperTranscriber:
    """faster-whisper with vad_filter=True: the Silero ONNX model it bundles drops
    non-speech, so taps and silence come back as '' (no 'Thank you.' hallucinations)."""

    def __init__(self, model_size: str, *, device: str, compute_type: str) -> None:
        from faster_whisper import WhisperModel

        # First use downloads the model (~150 MB for base.en) into the Hugging Face cache.
        self._model = WhisperModel(model_size, device=device, compute_type=compute_type)

    def transcribe(self, pcm: bytes) -> str:
        segments, _info = self._model.transcribe(
            pcm_to_float32(pcm),
            language="en",
            vad_filter=True,
            beam_size=1,  # greedy: fastest on CPU; revisit if accuracy is poor
            condition_on_previous_text=False,
        )
        return " ".join(seg.text.strip() for seg in segments).strip()
