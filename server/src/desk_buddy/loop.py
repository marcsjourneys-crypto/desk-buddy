"""The voice loop: push-to-talk events in, spoken replies out."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from enum import StrEnum
from typing import Protocol

from desk_buddy.audio import duration_seconds
from desk_buddy.llm import Chat, LLMError, LLMRefusal, SentenceSplitter
from desk_buddy.stt import Transcriber
from desk_buddy.tts import Synthesizer

log = logging.getLogger(__name__)

MIN_CLIP_SECONDS = 0.3
CANNED_ERROR = "Sorry, I can't reach my brain right now."
CANNED_REFUSAL = "Hmm, I'd better not answer that one."


class State(StrEnum):
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"


class Mic(Protocol):
    def start(self) -> None: ...
    def stop(self) -> bytes: ...


class Player(Protocol):
    def play(self, pcm: bytes, token: int) -> None: ...
    def stop(self) -> None: ...
    def current_token(self) -> int: ...


class Orchestrator:
    """State machine. One asyncio.Task per turn; a key press mid-turn cancels it."""

    def __init__(
        self,
        *,
        mic: Mic,
        transcriber: Transcriber,
        chat: Chat,
        synthesizer: Synthesizer,
        speaker: Player,
        on_state: Callable[[State], None] = lambda state: None,
    ) -> None:
        self.mic = mic
        self.transcriber = transcriber
        self.chat = chat
        self.synthesizer = synthesizer
        self.speaker = speaker
        self.state = State.IDLE
        self._on_state = on_state
        self._turn: asyncio.Task[None] | None = None
        self._t0 = 0.0

    # --- events ---

    async def run(self, events: asyncio.Queue[str]) -> None:
        while True:
            event = await events.get()
            try:
                await self.handle(event)
            except Exception:
                log.exception("Failed to handle %r event", event)

    async def handle(self, event: str) -> None:
        if event == "down":
            await self._interrupt()
            self.mic.start()
            self._set_state(State.LISTENING)
        elif event == "up" and self.state is State.LISTENING:
            pcm = self.mic.stop()
            seconds = duration_seconds(pcm)
            if seconds < MIN_CLIP_SECONDS:
                log.info("Ignored %.2f s tap", seconds)
                self._set_state(State.IDLE)
                return
            self._set_state(State.THINKING)
            self._t0 = time.perf_counter()
            self._turn = asyncio.create_task(self._run_turn(pcm))

    async def wait_idle(self) -> None:
        if self._turn:
            await self._turn

    async def shutdown(self) -> None:
        await self._interrupt()

    # --- internals ---

    def _set_state(self, state: State) -> None:
        if state is not self.state:
            self.state = state
            log.debug("state -> %s", state)
            self._on_state(state)

    def _ms(self) -> float:
        return (time.perf_counter() - self._t0) * 1000

    async def _interrupt(self) -> None:
        turn, self._turn = self._turn, None
        if turn and not turn.done():
            log.info("Interrupted")
            turn.cancel()
            self.speaker.stop()
            # wait() returns when the turn is done without re-raising its CancelledError,
            # but a cancel aimed at *this* task (Ctrl+C) still propagates.
            await asyncio.wait([turn])

    async def _run_turn(self, pcm: bytes) -> None:
        # Runs on the event loop after any _interrupt() has called stop(), so this
        # token is fresh; plays from older turns hold stale tokens and write nothing.
        token = self.speaker.current_token()
        try:
            # An interrupt during THINKING cancels this await but not the transcribe
            # thread: it runs to completion, its result is discarded, and it may briefly
            # compete for CPU with the next turn. Accepted for M1.
            text = await asyncio.to_thread(self.transcriber.transcribe, pcm)
            log.info("STT %.0f ms", self._ms())
            if not text:
                log.info("No speech detected")
                return
            log.info("You: %s", text)
            queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=2)
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self._produce(text, queue))
                tg.create_task(self._consume(queue, token))
        except Exception:
            log.exception("Turn failed")
        finally:
            if not asyncio.current_task().cancelling():
                self._set_state(State.IDLE)

    async def _produce(self, text: str, queue: asyncio.Queue[bytes | None]) -> None:
        """Claude deltas -> sentences -> PCM, into the queue. None marks the end."""
        splitter = SentenceSplitter()
        reply: list[str] = []
        first_token = True
        try:
            async with contextlib.aclosing(self.chat.stream_reply(text)) as deltas:
                async for delta in deltas:
                    if first_token:
                        log.info("First token %.0f ms", self._ms())
                        first_token = False
                    for sentence in splitter.feed(delta):
                        reply.append(sentence)
                        await self._enqueue(sentence, queue)
            for sentence in splitter.flush():
                reply.append(sentence)
                await self._enqueue(sentence, queue)
        except LLMRefusal:
            log.warning("Claude refused")
            reply.append(CANNED_REFUSAL)
            await self._enqueue(CANNED_REFUSAL, queue)
        except LLMError as exc:
            log.warning("LLM error: %s", exc)
            reply.append(CANNED_ERROR)
            await self._enqueue(CANNED_ERROR, queue)
        log.info("Buddy: %s", " ".join(reply))
        await queue.put(None)

    async def _enqueue(self, sentence: str, queue: asyncio.Queue[bytes | None]) -> None:
        await queue.put(await asyncio.to_thread(self.synthesizer.synthesize, sentence))

    async def _consume(self, queue: asyncio.Queue[bytes | None], token: int) -> None:
        while (pcm := await queue.get()) is not None:
            if self.state is not State.SPEAKING:
                log.info("First audio %.0f ms", self._ms())
                self._set_state(State.SPEAKING)
            await asyncio.to_thread(self.speaker.play, pcm, token)
