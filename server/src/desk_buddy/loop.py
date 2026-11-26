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
from typing import Any, Protocol

from desk_buddy.audio import duration_seconds
from desk_buddy.brain.emotion import DEFAULT_EMOTION, EmotionParser
from desk_buddy.brain.memory import Conversation, is_forget_command
from desk_buddy.brain.personality import Line, Personality
from desk_buddy.events import NULL_BUS
from desk_buddy.llm import Chat, LLMError, LLMRefusal, SentenceSplitter
from desk_buddy.stt import Transcriber
from desk_buddy.tools.confirm import QUESTION, Confirmations
from desk_buddy.tools.urls import SearchResults
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
    first_token_ms: int | None = None  # from key release, for the dashboard
    first_audio_ms: int | None = None
    note: str | None = None  # a system note sent after the user's words (a pending yes)
    asked: str | None = None  # the server's "Shall I open …?", once it started playing


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
        on_question: Callable[[], None] = lambda: None,
        bus: Any = NULL_BUS,
        personality: Callable[[], Personality] = Personality,
        confirmations: Confirmations | None = None,
        search_results: SearchResults | None = None,
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
        self._on_question = on_question  # a new turn: resets the per-question action limit
        self._turn: asyncio.Task[None] | None = None
        self._t0 = 0.0
        self._clock = clock
        self._random = random
        self._greeted_at: float | None = None
        self._pondered_at: float | None = None
        self._bus = bus  # live events for the dashboard (events.EventBus)
        self._personality = personality  # the current one: lines are read when spoken
        # An action waiting for the user's spoken yes: the loop asks about it after the
        # reply, and the next question's words answer it before Claude sees them.
        self._confirmations = confirmations
        # Web search result addresses of this question and the one before (the pages
        # open_search_result may open): each question with speech moves them along.
        self._search_results = search_results
        self._questions = 0  # questions with speech so far

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
            self._on_question()
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
            if self.say(self._line("farewell")) and self._turn is not None:
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
        self.say(self._line("greeting"), on_done=self._greeted)

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
            self._publish("state", name=state.value)
            self._on_state(state)

    def _line(self, name: str) -> Line:
        """A signature line as it is now (edited on the dashboard, or the default)."""
        try:
            return self._personality().line(name)
        except Exception:
            log.warning("Couldn't read the %s line; using the default", name, exc_info=True)
            return Personality().line(name)

    def _publish(self, type_: str, **fields: Any) -> None:
        """An event for the dashboard. Never raises into the loop."""
        try:
            self._bus.publish(type_, **fields)
        except Exception:
            log.debug("Dashboard event %s failed", type_, exc_info=True)

    def _ms(self) -> float:
        return (time.perf_counter() - self._t0) * 1000

    async def _interrupt(self) -> None:
        turn, self._turn = self._turn, None
        if turn and not turn.done():
            log.info("Interrupted")
            self._publish("interrupted")
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
            self._publish("heard", text=text, stt_ms=round(self._ms()))
            # A question with speech: number it (this task's tool threads inherit it), and
            # move the search results along (an empty clip isn't a question).
            self._questions += 1
            question = self._questions
            QUESTION.set(question)
            if self._search_results is not None:
                self._search_results.new_question(question)
            if is_forget_command(text):
                if self._confirmations is not None:
                    self._confirmations.expire(
                        "Forgotten (a fresh start).",
                        "was dropped when the user asked for a fresh start",
                    )
                if self._search_results is not None:
                    self._search_results.clear()
                self.conversation.forget()
                log.info("Memory cleared")
                await self._say(self._line("forget"), token)
                return
            answered = await self._answer_pending(text, question, token)
            # The last question's search results, numbered, so Claude can open one by
            # its number (it never sees earlier questions' raw results).
            listed = self._search_results.note() if self._search_results is not None else None
            note = " ".join(n for n in (answered, listed) if n) or None
            turn = _Turn(user=text, thinking_since=time.perf_counter(), note=note)
            tools_before = _tools_succeeded(self.chat)
            if hasattr(self.chat, "last_tool_rounds"):
                self.chat.last_tool_rounds = []  # never remember an earlier turn's tools
            if hasattr(self.chat, "searches"):
                self.chat.searches = []  # ...or its searches
            queue: asyncio.Queue[tuple[str, bytes] | None] = asyncio.Queue(maxsize=2)
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self._produce(turn, queue))
                tg.create_task(self._consume_reply(turn, queue, token))
            turn.complete = True
            asked = not turn.failed and await self._ask_pending(turn, question, token)
            if (
                not turn.failed
                and not asked
                and _tools_succeeded(self.chat) > tools_before
                and self._random() < TASK_DONE_CHANCE
            ):
                await self._say_extra(self._line("task_done"), token)
        except Exception:
            # A TaskGroup failure arrives as an ExceptionGroup, which is an Exception.
            log.exception("Turn failed")
            if turn:
                turn.failed = True
        finally:
            # Also runs when the turn is cancelled by an interrupt. An action proposed in
            # this turn that wasn't asked about to the end is dropped.
            if self._confirmations is not None:
                self._confirmations.cancel_unarmed("The user wasn't asked (cut off or failed).")
            if turn:
                self._remember(turn)
                if turn.spoken:  # what CARL actually said, even if it was cut off
                    self._publish(
                        "reply",
                        text=" ".join(turn.spoken),
                        emotion=turn.emotion or DEFAULT_EMOTION,
                        first_token_ms=turn.first_token_ms,
                        first_audio_ms=turn.first_audio_ms,
                    )
            if not asyncio.current_task().cancelling():
                self._set_state(State.IDLE)

    def _remember(self, turn: _Turn) -> None:
        """Keep what the user actually heard, plus every tool call that ran (the action
        happened even if the reply was cut off or failed), and the queries of any web
        searches (as a note; never the search blocks). A failed reply's canned error line
        isn't kept; a turn with neither a reply nor tools isn't kept at all. Searches
        don't count toward TASK_DONE: that stays tied to our own tools."""
        rounds = list(getattr(self.chat, "last_tool_rounds", None) or [])
        searches = [s.query for s in getattr(self.chat, "searches", None) or []]
        said = [*turn.spoken, *([turn.asked] if turn.asked else [])]  # the question too
        heard = said and not turn.failed
        if not heard and not rounds and not turn.note:
            return
        emotion = turn.emotion or ("curious" if turn.asked else DEFAULT_EMOTION)
        reply = f"[{emotion}] " + " ".join(said) if heard else None
        self.conversation.add(
            turn.user,
            reply,
            interrupted=not turn.complete and not turn.failed,
            tool_rounds=rounds,
            searches=searches,
            notes=[turn.note] if turn.note else [],
        )

    async def _answer_pending(self, text: str, question: int, token: int) -> str | None:
        """Run, cancel or expire an action waiting for a yes, on the user's words only
        (the transcript: nothing Claude, a tool or a web page says). Returns the system
        note that goes after the user's words, or None. Never raises into the turn. A
        confirmed action that takes over PONDER_AFTER seconds (a slow skill) gets
        PONDERING meanwhile (at most once every PONDER_EVERY seconds)."""
        confirmations = self._confirmations
        if confirmations is None:
            return None
        # Off the loop: a confirmed action may take a moment (opening a page, a skill).
        answering = asyncio.ensure_future(asyncio.to_thread(confirmations.answer, text, question))
        try:
            try:
                return await asyncio.wait_for(asyncio.shield(answering), PONDER_AFTER)
            except TimeoutError:
                pass
            await self._ponder(_Turn(user=text), token)
            self._set_state(State.THINKING)
            return await asyncio.shield(answering)
        except asyncio.CancelledError:
            # Cut off while the action runs: it still finishes, and its note reaches
            # Claude with the next question instead.
            answering.add_done_callback(lambda task: _report_late(confirmations, task))
            raise
        except Exception:
            log.exception("Couldn't answer the pending action")
            return None

    async def _ask_pending(self, turn: _Turn, question: int, token: int) -> bool:
        """If this turn proposed an action that needs a yes, the server (never the
        model) asks about it after the reply: "Shall I open the page on bbc.co.uk?". The
        action is armed as the question starts playing, so the user may cut it off to
        answer. If it can't be said, the action is cancelled (an interrupt before it
        starts cancels it in _run_turn). The question is part of the reply in memory: it
        was heard. True if it asked."""
        if self._confirmations is None:
            return False
        action = self._confirmations.to_ask(question)
        if action is None:
            return False
        line = Line("curious", action.ask)
        try:
            pcm = await asyncio.to_thread(self.synthesizer.synthesize, line.text)
        except Exception as exc:
            log.warning("Couldn't ask %r: %s", line.text, exc)
            self._confirmations.cancel_unarmed("The question couldn't be asked.")
            return True
        self._emit_emotion(line.emotion)
        log.info("CARL [%s]: %s", line.emotion, line.text)
        self._publish("line", text=line.text, emotion=line.emotion)
        self._confirmations.arm(action, question)  # from the moment it starts
        turn.asked = line.text
        turn.complete = False  # until the question has played to the end
        await self._play((line.text, pcm), token, [])
        turn.complete = True
        return True

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
            if turn.note:
                prompt = f"{prompt} {turn.note}"
            async with contextlib.aclosing(self.chat.stream_reply(history, prompt)) as deltas:
                async for delta in deltas:
                    if first_token:
                        log.info("First token %.0f ms", self._ms())
                        turn.first_token_ms = round(self._ms())
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
            refusal = self._line("refusal")
            self._show(turn, refusal.emotion)
            await speak([refusal.text])
        except LLMError as exc:
            log.warning("LLM error: %s", exc)
            turn.failed = True
            error = self._line("error")
            self._show(turn, error.emotion)
            await speak([error.text])
        said = " ".join(reply).strip()
        if said:  # a turn that ended at a call needing a yes may have said nothing
            log.info("CARL [%s]: %s", turn.emotion or DEFAULT_EMOTION, said)
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
        self._publish("emotion", name=emotion)
        self._on_emotion(emotion)

    async def _say(self, line: Line, token: int) -> None:
        """Speak one canned line (no Claude, nothing remembered)."""
        self._emit_emotion(line.emotion)
        log.info("CARL [%s]: %s", line.emotion, line.text)
        self._publish("line", text=line.text, emotion=line.emotion)
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
            turn.first_audio_ms = round(self._ms())
            await self._play(first, token, turn.spoken)
            await self._consume(queue, token, turn.spoken)

    async def _ponder(self, turn: _Turn, token: int) -> None:
        now = self._clock()
        if self._pondered_at is not None and now - self._pondered_at < PONDER_EVERY:
            return
        self._pondered_at = now
        turn.pondering = True
        try:
            await self._say_extra(self._line("pondering"), token)
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


def _report_late(confirmations: Confirmations, task: asyncio.Future[str | None]) -> None:
    """The answer to a yes whose turn was cut off has finished: hand its note on (or log
    why there's none). Never raises: it runs as a done-callback."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error("Couldn't answer the pending action", exc_info=exc)
        return
    note = task.result()
    if note:
        confirmations.report_late(note)


def _tools_succeeded(chat: Chat) -> int:
    """The chat's running count of successful tool calls (0 if it doesn't keep one)."""
    return getattr(chat, "tools_succeeded", 0)
