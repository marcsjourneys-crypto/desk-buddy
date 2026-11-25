"""The voice loop: push-to-talk events in, spoken replies out."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random as random_module
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from desk_buddy.audio import duration_seconds
from desk_buddy.brain.emotion import DEFAULT_EMOTION, EmotionParser
from desk_buddy.brain.memory import Conversation, is_forget_command
from desk_buddy.brain.personality import (
    ERROR_LINE,
    FAREWELL,
    FORGET_LINE,
    GREETING,
    PONDERING,
    REFUSAL_LINE,
    TASK_DONE,
    Line,
)
from desk_buddy.llm import Chat, LLMError, LLMRefusal, SentenceSplitter
from desk_buddy.stt import Transcriber
from desk_buddy.tts import Synthesizer

log = logging.getLogger(__name__)

MIN_CLIP_SECONDS = 0.3
GREET_GAP = 60.0  # seconds: a "greet" this soon after the last greeting is skipped
PONDER_AFTER = 4.0  # seconds after key release with no reply audio: say PONDERING...
PONDER_EVERY = 300.0  # ...but at most this often
TASK_DONE_CHANCE = 0.25  # after a reply whose turn ran a tool successfully


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


@dataclass
class _Turn:
    """What happened in one turn, for deciding what to remember."""

    user: str
    emotion: str | None = None
    spoken: list[str] = field(default_factory=list)  # sentences that started playing
    failed: bool = False
    complete: bool = False
    thinking_since: float = 0.0  # perf_counter when transcription ended
    shown: str | None = None  # the emotion the reply wants on the eyes now
    pondering: bool = False  # PONDERING is playing: hold the reply's emotion back


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
        conversation: Conversation | None = None,
        on_state: Callable[[State], None] = lambda state: None,
        on_emotion: Callable[[str], None] = lambda emotion: None,
        clock: Callable[[], float] = time.monotonic,
        random: Callable[[], float] = random_module.random,
    ) -> None:
        self.mic = mic
        self.transcriber = transcriber
        self.chat = chat
        self.synthesizer = synthesizer
        self.speaker = speaker
        self.state = State.IDLE
        self.conversation = Conversation() if conversation is None else conversation
        self._on_state = on_state
        self._on_emotion = on_emotion
        self._turn: asyncio.Task[None] | None = None
        self._t0 = 0.0
        self._clock = clock
        self._random = random
        self._greeted_at: float | None = None
        self._pondered_at: float | None = None

    # --- events ---

    async def run(self, events: asyncio.Queue[str]) -> None:
        """Handle events from the queue forever: "down", "up", "reset" and "greet"."""
        while True:
            event = await events.get()
            try:
                await self.handle(event)
            except Exception:
                log.exception("Failed to handle %r event", event)

    async def handle(self, event: str) -> None:
        """Events: "down" and "up" are the talk button; "reset" (a robot attached or
        detached) interrupts any turn, stops the mic if it was listening, and goes idle;
        "greet" (startup, or a robot connected) says GREETING unless it was said in the
        last GREET_GAP seconds."""
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
        elif event == "reset":
            # A robot attached or detached: drop whatever was happening and wait.
            await self._interrupt()
            try:
                if self.state is State.LISTENING:
                    self.mic.stop()
            finally:
                self._set_state(State.IDLE)
        elif event == "greet":
            self._greet()

    async def wait_idle(self) -> None:
        if self._turn:
            await self._turn

    async def shutdown(self) -> None:
        await self._interrupt()

    def say(self, line: Line, on_done: Callable[[], None] | None = None) -> bool:
        """Start speaking a canned line through the active device, with its emotion. It
        runs as a turn, so a key press interrupts it, and it is never remembered. Only
        when idle: returns False (and says nothing) otherwise. Doesn't wait for the end;
        `on_done` runs only if the line played to the end."""
        if self.state is not State.IDLE or (self._turn and not self._turn.done()):
            log.debug("Skipped %r: busy", line.text)
            return False
        self._t0 = time.perf_counter()
        self._turn = asyncio.create_task(self._say_turn(line, on_done))
        return True

    async def farewell(self, timeout: float) -> None:
        """Drop whatever is happening and say FAREWELL, waiting at most `timeout` seconds
        (then it is cut off). Never raises: shutdown must go on."""
        try:
            await self.handle("reset")
            if self.say(FAREWELL) and self._turn is not None:
                await asyncio.wait([self._turn], timeout=timeout)
            await self._interrupt()  # cut it off if it timed out
        except Exception:
            log.warning("Couldn't say farewell", exc_info=True)

    def _greet(self) -> None:
        now = self._clock()
        if self._greeted_at is not None and now - self._greeted_at < GREET_GAP:
            log.debug("Skipped greeting: greeted %.0f s ago", now - self._greeted_at)
            return
        # Counted only once it has played: an interrupted or failed greeting doesn't count.
        self.say(GREETING, on_done=self._greeted)

    def _greeted(self) -> None:
        self._greeted_at = self._clock()

    async def _say_turn(self, line: Line, on_done: Callable[[], None] | None = None) -> None:
        try:
            await self._say(line, self.speaker.current_token())
            if on_done is not None:
                on_done()
        except Exception:
            log.exception("Couldn't say %r", line.text)
        finally:
            if not asyncio.current_task().cancelling():
                self._set_state(State.IDLE)

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
        turn: _Turn | None = None
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
            if is_forget_command(text):
                self.conversation.forget()
                log.info("Memory cleared")
                await self._say(FORGET_LINE, token)
                return
            turn = _Turn(user=text, thinking_since=time.perf_counter())
            tools_before = _tools_succeeded(self.chat)
            queue: asyncio.Queue[tuple[str, bytes] | None] = asyncio.Queue(maxsize=2)
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self._produce(turn, queue))
                tg.create_task(self._consume_reply(turn, queue, token))
            turn.complete = True
            if (
                not turn.failed
                and _tools_succeeded(self.chat) > tools_before
                and self._random() < TASK_DONE_CHANCE
            ):
                await self._say_extra(TASK_DONE, token)
        except Exception:
            # A TaskGroup failure arrives as an ExceptionGroup, which is an Exception.
            log.exception("Turn failed")
            if turn:
                turn.failed = True
        finally:
            # Also runs when the turn is cancelled by an interrupt.
            if turn:
                self._remember(turn)
            if not asyncio.current_task().cancelling():
                self._set_state(State.IDLE)

    def _remember(self, turn: _Turn) -> None:
        """Keep what the user actually heard; drop failed turns and silent ones."""
        if turn.failed or not turn.spoken:
            return
        reply = f"[{turn.emotion or DEFAULT_EMOTION}] " + " ".join(turn.spoken)
        self.conversation.add(turn.user, reply, interrupted=not turn.complete)

    async def _produce(self, turn: _Turn, queue: asyncio.Queue[tuple[str, bytes] | None]) -> None:
        """Claude deltas -> emotion + sentences -> PCM, into the queue. None marks the end."""
        parser = EmotionParser()
        splitter = SentenceSplitter()
        reply: list[str] = []
        first_token = True

        async def speak(sentences: list[str]) -> None:
            for sentence in sentences:
                reply.append(sentence)
                await self._enqueue(sentence, queue)

        try:
            history = self.conversation.messages()
            prompt = self.conversation.user_message(turn.user)
            async with contextlib.aclosing(self.chat.stream_reply(history, prompt)) as deltas:
                async for delta in deltas:
                    if first_token:
                        log.info("First token %.0f ms", self._ms())
                        first_token = False
                    text = parser.feed(delta)
                    self._note_emotion(turn, parser.emotion)
                    await speak(splitter.feed(text))
            text = parser.flush()
            self._note_emotion(turn, parser.emotion)
            await speak(splitter.feed(text) + splitter.flush())
        except LLMRefusal:
            log.warning("Claude refused")
            turn.failed = True
            self._show(turn, REFUSAL_LINE.emotion)
            await speak([REFUSAL_LINE.text])
        except LLMError as exc:
            log.warning("LLM error: %s", exc)
            turn.failed = True
            self._show(turn, ERROR_LINE.emotion)
            await speak([ERROR_LINE.text])
        log.info("CARL [%s]: %s", turn.emotion or DEFAULT_EMOTION, " ".join(reply))
        await queue.put(None)

    def _note_emotion(self, turn: _Turn, emotion: str | None) -> None:
        if emotion and turn.emotion is None:
            turn.emotion = emotion
            self._show(turn, emotion)

    def _show(self, turn: _Turn, emotion: str) -> None:
        """Send the reply's emotion, unless PONDERING is playing: then it's sent after."""
        turn.shown = emotion
        if not turn.pondering:
            self._emit_emotion(emotion)

    def _emit_emotion(self, emotion: str) -> None:
        self._on_emotion(emotion)

    async def _say(self, line: Line, token: int) -> None:
        """Speak one canned line (no Claude, nothing remembered)."""
        self._emit_emotion(line.emotion)
        log.info("CARL [%s]: %s", line.emotion, line.text)
        queue: asyncio.Queue[tuple[str, bytes] | None] = asyncio.Queue()
        await self._enqueue(line.text, queue)
        await queue.put(None)
        await self._consume(queue, token, [])

    async def _say_extra(self, line: Line, token: int) -> None:
        """A canned line on top of a reply (PONDERING, TASK_DONE). If it fails, it's
        skipped with a warning: it must never cost the real reply. A cancel (interrupt)
        still propagates."""
        try:
            await self._say(line, token)
        except Exception as exc:
            log.warning("Skipped %r: %s", line.text, exc)

    async def _enqueue(self, sentence: str, queue: asyncio.Queue[tuple[str, bytes] | None]) -> None:
        pcm = await asyncio.to_thread(self.synthesizer.synthesize, sentence)
        await queue.put((sentence, pcm))

    async def _consume_reply(
        self, turn: _Turn, queue: asyncio.Queue[tuple[str, bytes] | None], token: int
    ) -> None:
        """Play the reply. If no sentence is ready PONDER_AFTER seconds after CARL started
        thinking (transcription ended), say PONDERING first (at most once every
        PONDER_EVERY seconds)."""
        wait = PONDER_AFTER - (time.perf_counter() - turn.thinking_since)
        try:
            first = await asyncio.wait_for(queue.get(), max(wait, 0))
        except TimeoutError:
            await self._ponder(turn, token)
            first = await queue.get()
        if first is not None:
            log.info("First audio %.0f ms", self._ms())  # the reply's, not a canned line's
            await self._play(first, token, turn.spoken)
            await self._consume(queue, token, turn.spoken)

    async def _ponder(self, turn: _Turn, token: int) -> None:
        now = self._clock()
        if self._pondered_at is not None and now - self._pondered_at < PONDER_EVERY:
            return
        self._pondered_at = now
        turn.pondering = True
        try:
            await self._say_extra(PONDERING, token)
        finally:
            turn.pondering = False
        if turn.shown:  # the reply's emotion, held back (or covered) by the line
            self._emit_emotion(turn.shown)

    async def _consume(
        self, queue: asyncio.Queue[tuple[str, bytes] | None], token: int, spoken: list[str]
    ) -> None:
        while (item := await queue.get()) is not None:
            await self._play(item, token, spoken)

    async def _play(self, item: tuple[str, bytes], token: int, spoken: list[str]) -> None:
        sentence, pcm = item
        self._set_state(State.SPEAKING)
        spoken.append(sentence)  # before play: an interrupt mid-sentence still counts it
        await asyncio.to_thread(self.speaker.play, pcm, token)


def _tools_succeeded(chat: Chat) -> int:
    """The chat's running count of successful tool calls (0 if it doesn't keep one)."""
    return getattr(chat, "tools_succeeded", 0)
