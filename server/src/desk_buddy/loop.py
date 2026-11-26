"""The voice loop: push-to-talk and wake word events in, spoken replies out."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random as random_module
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from desk_buddy import vad
from desk_buddy.audio import duration_seconds
from desk_buddy.brain.emotion import DEFAULT_EMOTION, EmotionParser
from desk_buddy.brain.memory import Conversation, is_forget_command
from desk_buddy.brain.personality import Line, Personality
from desk_buddy.brain.smart import UNAVAILABLE, SmartRouter
from desk_buddy.events import NULL_BUS
from desk_buddy.llm import Chat, LLMError, LLMRefusal, SentenceSplitter
from desk_buddy.stt import Transcriber
from desk_buddy.tools.confirm import QUESTION, Confirmations
from desk_buddy.tools.urls import SearchResults
from desk_buddy.tts import Synthesizer
from desk_buddy.vad import TERMINAL, Endpointer, Event

log = logging.getLogger(__name__)

MIN_CLIP_SECONDS = 0.3
GREET_GAP = 60.0  # seconds: a "greet" this soon after the last greeting is skipped
PONDER_AFTER = 4.0  # seconds after key release with no reply audio: say PONDERING...
PONDER_EVERY = 300.0  # ...but at most this often
TASK_DONE_CHANCE = 0.25  # after a reply whose turn ran a tool successfully
FOLLOW_UP_SECONDS = 5.0  # listening after a reply to a hands-free question (0: off)
WAKE_END_SILENCE = 0.8  # a hands-free question ends after this much silence
WAKE_NO_SPEECH = 4.0  # no speech this long after a wake: a false wake
WAKE_MAX_SECONDS = 15.0  # a hands-free question is cut off here
# The follow-up starts this long after the reply's last play() returns. The robot's
# player returns about 0.2 s before its audio ends (it sends ahead), and every sentence
# now ends with tts.SENTENCE_PAUSE_S (0.25 s) of silence, so the speaker is quiet by
# then. Was 0.7: with the pause that opened the mic ~1 s after CARL stopped talking, and
# a quick "yes" to "Shall I open…?" was lost (a user check, 2026-11-26).
FOLLOW_UP_DELAY = 0.15
# A wake recording starts with ~0.3 s from before the detection (the end of "CARL"):
# speech there doesn't start a question. Follow-ups have no phrase.
WAKE_IGNORE_FIRST = 0.3
MAX_FOLLOW_UPS = 5  # follow-ups in a row; then only a wake or the key (the user raised it from 3)
# Hands-free listening stops if the robot sends no audio for this long, or if it
# lasts EXTRA_SECONDS longer than the detector could ever take (wall clock).
FRAME_GAP = 1.5
EXTRA_SECONDS = 2.0
# How a yes heard with no key held is labelled on the action (log and dashboard).
_YES_VIA = {"wake": "yes after wake", "follow_up": "yes in follow-up"}
# The wake phrase (or the end of it) at the start of a wake-started transcript.
_GREETING = r"(?:(?:hey|hi|okay|ok)\W+)?"
_WAKE_PHRASE = re.compile(rf"^\W*{_GREETING}(?:carl|karl)(?![\w'])\W*", re.IGNORECASE)
# A transcript that is only the phrase, as Whisper may hear it ("Call.").
_ONLY_PHRASE = re.compile(rf"^\W*{_GREETING}(?:carl|karl|call|cal)\W*$", re.IGNORECASE)


def strip_wake_phrase(text: str) -> str:
    """The transcript of a wake-started question without a leading "Carl" or "Karl",
    optionally after "hey", "hi", "okay" or "ok" (any case, with the punctuation after
    it). Empty if it was only the phrase (also "Call." or "Cal"). A real name at the
    start is lost too: "Karl Marx wrote..." becomes "Marx wrote...". Only wake-started
    questions are stripped."""
    if _ONLY_PHRASE.match(text):
        return ""
    return _WAKE_PHRASE.sub("", text, count=1).strip()


class State(StrEnum):
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"


class Mic(Protocol):
    def start(self) -> None: ...
    def stop(self) -> bytes: ...


class HandsFreeMic(Mic, Protocol):
    """A mic that can record with no key held (the robot's, through the DeviceSwitch)."""

    def begin_hands_free(self, on_frame: Callable[[bytes], None]) -> bool: ...
    def end_hands_free(self, *, keep_recording: bool = False) -> None: ...


@dataclass(frozen=True)
class HandsFree:
    """The wake word and follow-up settings, read once at each wake and follow-up."""

    enabled: bool = True  # WAKE_WORD: off ignores wakes (and starts no follow-ups)
    follow_up_seconds: float = FOLLOW_UP_SECONDS
    end_silence: float = WAKE_END_SILENCE
    no_speech_timeout: float = WAKE_NO_SPEECH
    max_seconds: float = WAKE_MAX_SECONDS
    follow_up_delay: float = FOLLOW_UP_DELAY
    ignore_first: float = WAKE_IGNORE_FIRST
    frame_gap: float = FRAME_GAP
    max_follow_ups: int = MAX_FOLLOW_UPS


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
        hands_free: Callable[[], HandsFree] = HandsFree,
        endpointer: Callable[..., Endpointer] = Endpointer,
        smart_router: SmartRouter | None = None,
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
        # When a question goes to the smart model (a phrase, or the stay after a smart
        # answer). Lives here, not in the chat, which is rebuilt between turns.
        self._smart = smart_router
        # Hands-free listening (after a wake word, or the follow-up after a reply): its
        # kind ("wake" or "follow_up"), its end-of-speech detector, and a generation that
        # makes the detector's late results stale once it is stopped.
        self._hands_free = hands_free
        self._make_endpointer = endpointer
        self._listening_kind: str | None = None
        self._endpointer: Endpointer | None = None
        self._hands_free_gen = 0
        self._events: asyncio.Queue[str] | None = None
        self._posting: set[asyncio.Task[None]] = set()
        self._settings = HandsFree()  # read at each hands-free start, kept until it ends
        self._timers: list[asyncio.TimerHandle] = []  # the time limit and the watchdog
        self._last_frame = 0.0  # loop time of the last robot mic frame
        self._follow_ups = 0  # follow-up windows in a row
        self._warned_no_model = False

    # --- events ---

    async def run(self, events: asyncio.Queue[str]) -> None:
        """Handle events from the queue forever: "down", "up", "reset", "greet", "stop",
        "forget" and "wake <score>", plus the loop's own "endpoint <generation> <result>"."""
        self._events = events
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
        last GREET_GAP seconds; "wake <score>" (the robot heard the wake word) starts a
        hands-free question when idle; "stop" (the dashboard) ends whatever is going on
        and goes idle, and "forget" does the same, then forgets like the spoken "forget
        that" and says FORGET_LINE. The key always wins over hands-free listening."""
        if event == "down":
            # The robot's mic already dropped the tap when the key began its recording
            # (RobotMic.begin), so no frame after the press reaches the detector. One
            # narrow race is left: if the detector ended the question in the same moment
            # (its "endpoint" is queued before this "down"), the key's begin() emptied
            # that clip, so the wake question is dropped as a tap and the key's question
            # goes on as usual. The key still wins.
            self._follow_ups = 0
            self._stop_hands_free(keep_recording=True, why="the key took over")
            await self._interrupt()
            self.mic.start()
            self._set_state(State.LISTENING)
        elif event == "up" and self.state is State.LISTENING and self._listening_kind is None:
            self._end_question(None)
        elif event.startswith("wake "):
            self._wake(float(event.split()[1]))
        elif event.startswith("endpoint "):
            _, gen, name = event.split()
            self._endpoint(int(gen), name)
        elif event == "reset":
            # A robot attached or detached: drop whatever was happening and wait.
            self._stop_hands_free(why="the robot link changed")
            await self._interrupt()
            try:
                if self.state is State.LISTENING:
                    self.mic.stop()
            finally:
                self._set_state(State.IDLE)
        elif event == "greet":
            self._greet()
        elif event in ("stop", "forget"):
            await self._stop()
            if event == "forget":
                self._forget_everything()
                self.say(self._line("forget"))
            self._publish("control", action=event)

    async def _stop(self) -> None:
        """Stop from the dashboard: end any hands-free listening, interrupt a turn (a
        reply, PONDERING, the yes question, a canned line), drop a recording in progress
        without transcribing it, and go idle. A pending yes is left to expire as usual.
        A later "up" finds CARL idle and does nothing."""
        log.info("Stopped from the dashboard")
        self._follow_ups = 0
        self._stop_hands_free(why="stopped from the dashboard")
        await self._interrupt()
        try:
            if self.state is State.LISTENING:
                self.mic.stop()  # the clip is dropped
        finally:
            self._set_state(State.IDLE)

    def _forget_everything(self) -> None:
        """A fresh start (the spoken forget, or the dashboard's): expire a pending yes,
        clear the search results, the conversation and the smart model's stay."""
        if self._confirmations is not None:
            self._confirmations.expire(
                "Forgotten (a fresh start).",
                "was dropped when the user asked for a fresh start",
            )
        if self._search_results is not None:
            self._search_results.clear()
        self.conversation.forget()
        if self._smart is not None:
            self._smart.forget()
        log.info("Memory cleared")

    def _end_question(self, kind: str | None) -> None:
        """The recording is over (key release, or end of speech): start the turn."""
        pcm = self.mic.stop()
        seconds = duration_seconds(pcm)
        if seconds < MIN_CLIP_SECONDS:
            log.info("Ignored %.2f s tap", seconds)
            self._set_state(State.IDLE)
            return
        self._set_state(State.THINKING)
        self._t0 = time.perf_counter()
        self._on_question()
        self._turn = asyncio.create_task(self._run_turn(pcm, kind))

    # --- hands-free: the wake word and the follow-up ---

    def _wake(self, score: float) -> None:
        if self.state is not State.IDLE or (self._turn and not self._turn.done()):
            log.debug("Ignored a wake (score %.2f): CARL is %s", score, self.state)
            return
        if vad.model_failed():
            if not self._warned_no_model:
                self._warned_no_model = True
                log.warning("Ignored a wake: the end-of-speech model didn't load")
            return
        settings = self._read_hands_free()
        if settings is None or not settings.enabled:
            log.debug("Ignored a wake (score %.2f): the wake word is off", score)
            return
        if not self._start_hands_free("wake", settings, settings.no_speech_timeout):
            log.debug("Ignored a wake (score %.2f): no robot owns the mic", score)
            return
        self._follow_ups = 0
        log.info("Wake (score %.2f): listening", score)
        self._publish("wake", score=score)

    def _read_hands_free(self) -> HandsFree | None:
        try:
            return self._hands_free()
        except Exception:
            log.exception("Couldn't read the wake word settings")
            return None

    def _start_hands_free(self, kind: str, settings: HandsFree, no_speech: float) -> bool:
        """Open the robot's mic with the end-of-speech tap, then go to `listening` (in
        that order, so the robot's pre-roll isn't dropped). False if it can't. Two
        wall-clock guards end it if the robot stops sending audio (FRAME_GAP) or it
        lasts longer than the detector ever could."""
        begin = getattr(self.mic, "begin_hands_free", None)
        if begin is None:
            return False
        try:
            endpointer = self._make_endpointer(
                end_silence=settings.end_silence,
                no_speech_timeout=no_speech,
                max_seconds=settings.max_seconds,
                ignore_first=settings.ignore_first if kind == "wake" else 0.0,
            )
        except Exception:
            log.exception("Couldn't start end-of-speech detection")
            return False
        self._hands_free_gen += 1
        gen = self._hands_free_gen
        self._endpointer = endpointer
        if not begin(lambda pcm: self._on_frame(gen, pcm)):
            self._endpointer = None
            return False
        self._listening_kind = kind
        self._settings = settings
        loop = asyncio.get_running_loop()
        self._last_frame = loop.time()
        limit = no_speech + settings.end_silence + settings.max_seconds + EXTRA_SECONDS
        self._timers = [
            loop.call_later(limit, self._time_out, gen),
            loop.call_later(settings.frame_gap, self._watch_frames, gen),
        ]
        self._set_state(State.LISTENING)
        return True

    def _watch_frames(self, gen: int) -> None:
        if gen != self._hands_free_gen or self._endpointer is None:
            return
        loop = asyncio.get_running_loop()
        quiet = loop.time() - self._last_frame
        if quiet >= self._settings.frame_gap:
            self._time_out(gen)
        else:
            self._timers.append(
                loop.call_later(self._settings.frame_gap - quiet, self._watch_frames, gen)
            )

    def _time_out(self, gen: int) -> None:
        if gen != self._hands_free_gen or self._endpointer is None:
            return
        self._finish_recording(gen, "timeout")

    def _cancel_timers(self) -> None:
        timers, self._timers = self._timers, []
        for timer in timers:
            timer.cancel()

    def _on_frame(self, gen: int, pcm: bytes) -> None:
        """Each recorded robot mic frame, on the event loop. Never raises. A result that
        ends the question goes through the event queue, so it keeps its order with the
        key's events."""
        endpointer = self._endpointer
        if gen != self._hands_free_gen or endpointer is None:
            return
        self._last_frame = asyncio.get_running_loop().time()
        try:
            event = endpointer.feed(pcm)
        except Exception:
            log.exception("End-of-speech detection failed")
            name = "error"
        else:
            if event is Event.SPEECH_START:
                log.debug("Speech started")
            if event not in TERMINAL:
                return
            name = event.value
        self._finish_recording(gen, name)

    def _finish_recording(self, gen: int, name: str) -> None:
        """The clip ends here, at the frame (or moment) that ended it: frames arriving
        before the queue gets to the result, such as the rest of a pre-roll burst, aren't
        added. The result goes through the queue, in order with the key's events."""
        self._endpointer = None
        self._cancel_timers()
        try:
            self.mic.end_hands_free()  # type: ignore[attr-defined]
        except Exception:
            log.exception("Couldn't end the hands-free recording")
        self._post(f"endpoint {gen} {name}")

    def _post(self, event: str) -> None:
        if self._events is not None:
            self._events.put_nowait(event)
            return
        task = asyncio.get_running_loop().create_task(self.handle(event))  # no run()
        self._posting.add(task)
        task.add_done_callback(self._posting.discard)

    def _endpoint(self, gen: int, name: str) -> None:
        if gen != self._hands_free_gen or self._listening_kind is None:
            return  # stale: the key took over, or a reset
        kind = self._listening_kind
        self._stop_hands_free()
        if name in (Event.END.value, Event.MAX.value):
            if name == Event.MAX.value:
                log.info("Question cut off at the %.0f s cap", self._settings.max_seconds)
            if kind == "follow_up":
                self._publish("follow_up", active=False, heard=True)
            self._end_question(kind)
            return
        try:
            self.mic.stop()  # the clip is dropped
        finally:
            if name == Event.NO_SPEECH.value and kind == "wake":
                log.info("False wake: no speech")
                self._publish("false_wake")
            elif name == Event.NO_SPEECH.value:
                log.info("Follow-up ended: no speech")
            elif name == "timeout":
                log.warning("Hands-free listening stopped: no audio from the robot")
            else:
                log.warning("Hands-free listening stopped: end-of-speech detection failed")
            if kind == "follow_up":
                self._publish("follow_up", active=False, heard=False)
            self._set_state(State.IDLE)

    def _stop_hands_free(self, *, keep_recording: bool = False, why: str = "") -> None:
        """Stop the tap and the detector (and end the recording unless keep_recording).
        Later detector results are stale. Does nothing unless listening hands-free."""
        kind = self._listening_kind
        if kind is None:
            return
        self._listening_kind = None
        self._endpointer = None
        self._hands_free_gen += 1
        self._cancel_timers()
        try:
            self.mic.end_hands_free(keep_recording=keep_recording)  # type: ignore[attr-defined]
        except Exception:
            log.exception("Couldn't stop hands-free listening")
        if why:
            log.info("Hands-free listening stopped: %s", why)
            if kind == "follow_up":
                self._publish("follow_up", active=False, heard=False)

    async def _follow_up(self) -> None:
        """After a reply to a hands-free question, listen again for a short while, after
        a short gap. Runs at the end of the turn's task, so a key press (an interrupt)
        cancels it like the reply."""
        try:
            settings = self._read_hands_free()
            if (
                settings is None
                or not settings.enabled
                or settings.follow_up_seconds <= 0
                or self._follow_ups >= settings.max_follow_ups
            ):
                if settings is not None and self._follow_ups >= settings.max_follow_ups > 0:
                    log.info("No follow-up: %d in a row", self._follow_ups)
                self._set_state(State.IDLE)
                return
            seconds = settings.follow_up_seconds
            await asyncio.sleep(settings.follow_up_delay)
            again = self._read_hands_free()  # the wake word may have been turned off
            if again is None or not again.enabled:
                self._set_state(State.IDLE)
                return
            if not self._start_hands_free("follow_up", settings, seconds):
                self._set_state(State.IDLE)
                return
            self._follow_ups += 1
            log.info("Follow-up: listening for %.0f s", seconds)
            self._publish("follow_up", active=True, seconds=seconds)
        except Exception:
            log.exception("Couldn't start the follow-up")
            self._stop_hands_free()
            self._set_state(State.IDLE)

    def end_hands_free(self, why: str) -> None:
        """End a wake recording or a follow-up in progress (the wake word was turned
        off): the clip is dropped and CARL goes idle. Key recordings and turns are left
        alone. On the event loop."""
        if self._listening_kind is None:
            return
        self._stop_hands_free(why=why)
        try:
            self.mic.stop()  # the clip is dropped
        finally:
            self._set_state(State.IDLE)

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

    async def _run_turn(self, pcm: bytes, kind: str | None = None) -> None:
        # Runs on the event loop after any _interrupt() has called stop(), so this
        # token is fresh; plays from older turns hold stale tokens and write nothing.
        # `kind` is how the question started: None (the key), "wake" or "follow_up". A
        # hands-free question whose reply wasn't cut off gets the follow-up window.
        token = self.speaker.current_token()
        turn: _Turn | None = None
        replied = False
        try:
            # An interrupt during THINKING cancels this await but not the transcribe
            # thread: it runs to completion, its result is discarded, and it may briefly
            # compete for CPU with the next turn. Accepted for M1.
            text = await asyncio.to_thread(self.transcriber.transcribe, pcm)
            log.info("STT %.0f ms", self._ms())
            if kind == "wake":
                question_text = strip_wake_phrase(text)
                if not question_text:  # only the wake phrase (or nothing): a false wake
                    log.info("False wake: heard only %r", text)
                    self._publish("false_wake")
                    return
                text = question_text
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
                self._forget_everything()
                await self._say(self._line("forget"), token)
                replied = True
                return
            answered = await self._answer_pending(text, question, token, kind)
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
            if self._smart is not None and hasattr(self.chat, "smart_route"):
                self.chat.smart_route = self._smart.route(text)
            if hasattr(self.chat, "answered_by"):
                self.chat.answered_by = None  # never an earlier turn's model
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
            replied = not turn.failed  # no follow-up after an error line or a refusal
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
                answered_by = getattr(self.chat, "answered_by", None)
                self._note_answer(turn, answered_by)
                if turn.spoken:  # what CARL actually said, even if it was cut off
                    smart = (
                        {"model": answered_by[0], "route": answered_by[1]} if answered_by else {}
                    )
                    self._publish(
                        "reply",
                        text=" ".join(turn.spoken),
                        emotion=turn.emotion or DEFAULT_EMOTION,
                        first_token_ms=turn.first_token_ms,
                        first_audio_ms=turn.first_audio_ms,
                        **smart,
                    )
            if not asyncio.current_task().cancelling():
                if replied and kind is not None:
                    await self._follow_up()
                else:
                    self._set_state(State.IDLE)

    def _note_answer(self, turn: _Turn, answered_by: tuple[str, str] | None) -> None:
        """Tell the router who answered, so a smart answer starts (or restarts) the stay
        and a Haiku one ends it. A reply that was heard counts even if it was cut off (or
        ended at the server's yes question); a failed turn, or one cut off before CARL
        said anything, leaves the stay as it was."""
        if self._smart is None or turn.failed:
            return
        if turn.complete or turn.spoken or turn.asked:
            self._smart.answered(answered_by is not None and answered_by[1] != UNAVAILABLE)

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

    async def _answer_pending(
        self, text: str, question: int, token: int, kind: str | None = None
    ) -> str | None:
        """Run, cancel or expire an action waiting for a yes, on the user's words only
        (the transcript: nothing Claude, a tool or a web page says). Returns the system
        note that goes after the user's words, or None. Never raises into the turn. A
        confirmed action that takes over PONDER_AFTER seconds (a slow skill) gets
        PONDERING meanwhile (at most once every PONDER_EVERY seconds)."""
        confirmations = self._confirmations
        if confirmations is None:
            return None
        # Off the loop: a confirmed action may take a moment (opening a page, a skill).
        via = _YES_VIA.get(kind) if kind else None
        answering = asyncio.ensure_future(
            asyncio.to_thread(confirmations.answer, text, question, via)
        )
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
