import asyncio
import logging
import threading
import time

import pytest

import desk_buddy.loop as loop_module
from desk_buddy.audio import BYTES_PER_SECOND
from desk_buddy.brain.memory import INTERRUPTED_NOTE, Conversation
from desk_buddy.brain.personality import (
    ERROR_LINE,
    FAREWELL,
    FORGET_LINE,
    GREETING,
    PONDERING,
    REFUSAL_LINE,
    TASK_DONE,
)
from desk_buddy.llm import LLMError, LLMRefusal
from desk_buddy.loop import Orchestrator, State

ONE_SECOND = b"\x00" * BYTES_PER_SECOND


class FakeMic:
    def __init__(self, pcm: bytes = ONE_SECOND):
        self.pcm, self.starts, self.stops = pcm, 0, 0

    def start(self):
        self.starts += 1

    def stop(self) -> bytes:
        self.stops += 1
        return self.pcm


class FakeTranscriber:
    def __init__(self, text: str = "hi buddy"):
        self.text, self.calls = text, 0

    def transcribe(self, pcm: bytes) -> str:
        self.calls += 1
        return self.text


class FakeChat:
    def __init__(self, deltas=("Hello there. ", "How are you?"), error: Exception | None = None):
        self.deltas, self.error, self.prompts = deltas, error, []
        self.histories = []

    async def stream_reply(self, history, text: str):
        self.prompts.append(text)
        self.histories.append(list(history))
        for d in self.deltas:
            yield d
        if self.error:
            raise self.error


class FakeSynth:
    def synthesize(self, text: str) -> bytes:
        return text.encode()


class FakeSpeaker:
    def __init__(self, block: bool = False):
        self.played: list[str] = []
        self.tokens: list[int] = []
        self.stops = 0
        self.block = block
        self.started = threading.Event()
        self._release = threading.Event()

    def current_token(self) -> int:
        return self.stops

    def play(self, pcm: bytes, token: int) -> None:
        self.played.append(pcm.decode())
        self.tokens.append(token)
        self.started.set()
        if self.block:
            self._release.wait(timeout=5)

    def stop(self) -> None:
        self.stops += 1
        self._release.set()


def make(**overrides):
    parts = {
        "mic": FakeMic(),
        "transcriber": FakeTranscriber(),
        "chat": FakeChat(),
        "synthesizer": FakeSynth(),
        "speaker": FakeSpeaker(),
        "conversation": Conversation(),
    }
    parts.update(overrides)
    states: list[State] = []
    emotions: list[str] = []
    orch = Orchestrator(**parts, on_state=states.append, on_emotion=emotions.append)
    parts["emotions"] = emotions
    return orch, parts, states


async def talk(orch: Orchestrator) -> None:
    await orch.handle("down")
    await orch.handle("up")
    await orch.wait_idle()


def test_normal_turn():
    orch, parts, states = make()
    asyncio.run(talk(orch))
    assert states == [State.LISTENING, State.THINKING, State.SPEAKING, State.IDLE]
    assert parts["chat"].prompts == ["hi buddy"]
    assert parts["speaker"].played == ["Hello there.", "How are you?"]
    assert parts["speaker"].tokens == [0, 0]


def test_turn_after_interrupt_uses_new_token():
    speaker = FakeSpeaker(block=True)
    orch, _, _ = make(speaker=speaker)

    async def scenario():
        await orch.handle("down")
        await orch.handle("up")
        assert await asyncio.to_thread(speaker.started.wait, 2)
        await orch.handle("down")  # interrupt -> stop() bumps the token
        speaker.block = False
        await orch.handle("up")
        await orch.wait_idle()

    asyncio.run(scenario())
    assert speaker.tokens == [0, 1, 1]


def test_short_tap_is_ignored():
    orch, parts, states = make(mic=FakeMic(pcm=b"\x00" * 100))
    asyncio.run(talk(orch))
    assert states == [State.LISTENING, State.IDLE]
    assert parts["transcriber"].calls == 0


def test_no_speech_goes_idle_without_calling_claude():
    orch, parts, states = make(transcriber=FakeTranscriber(text=""))
    asyncio.run(talk(orch))
    assert states == [State.LISTENING, State.THINKING, State.IDLE]
    assert parts["chat"].prompts == []


def test_api_error_speaks_canned_line():
    orch, parts, states = make(chat=FakeChat(deltas=(), error=LLMError("boom")))
    asyncio.run(talk(orch))
    assert parts["speaker"].played == [ERROR_LINE.text]
    assert states[-1] is State.IDLE


def test_error_after_partial_reply_keeps_spoken_part():
    orch, parts, _ = make(chat=FakeChat(deltas=("First. ",), error=LLMError("boom")))
    asyncio.run(talk(orch))
    assert parts["speaker"].played == ["First.", ERROR_LINE.text]


def test_refusal_speaks_refusal_line():
    orch, parts, _ = make(chat=FakeChat(deltas=(), error=LLMRefusal("no")))
    asyncio.run(talk(orch))
    assert parts["speaker"].played == [REFUSAL_LINE.text]


def test_up_without_down_is_ignored():
    orch, parts, states = make()
    asyncio.run(orch.handle("up"))
    assert states == []
    assert parts["transcriber"].calls == 0


def test_key_down_while_speaking_interrupts_and_listens():
    speaker = FakeSpeaker(block=True)
    orch, parts, states = make(speaker=speaker)

    async def scenario():
        await orch.handle("down")
        await orch.handle("up")
        assert await asyncio.to_thread(speaker.started.wait, 2)
        await orch.handle("down")  # interrupt
        assert orch.state is State.LISTENING

    asyncio.run(scenario())
    assert speaker.stops == 1
    assert speaker.played == ["Hello there."]  # second sentence never played
    assert parts["mic"].starts == 2
    # the cancelled turn must not flash IDLE before the new LISTENING
    assert states == [State.LISTENING, State.THINKING, State.SPEAKING, State.LISTENING]


class FlakyMic(FakeMic):
    """start() raises the first time only."""

    def start(self):
        super().start()
        if self.starts == 1:
            raise OSError("device busy")


class HoldingChat:
    """Yields one sentence, then waits forever; when cancelled, holds the turn open
    (in its finally) until the test releases it."""

    def __init__(self):
        self.cancelled = asyncio.Event()
        self.hold = asyncio.Event()

    async def stream_reply(self, history, text: str):
        yield "Hello there. "
        try:
            await asyncio.Event().wait()
        finally:
            self.cancelled.set()
            await self.hold.wait()


def test_cancelling_run_during_interrupt_propagates():
    speaker = FakeSpeaker(block=True)

    async def scenario():
        chat = HoldingChat()
        orch, parts, _ = make(speaker=speaker, chat=chat)
        events: asyncio.Queue[str] = asyncio.Queue()
        runner = asyncio.create_task(orch.run(events))
        events.put_nowait("down")
        events.put_nowait("up")
        assert await asyncio.to_thread(speaker.started.wait, 2)
        turn = orch._turn
        try:
            events.put_nowait("down")  # interrupt; the turn stays alive until hold is set
            await asyncio.wait_for(chat.cancelled.wait(), 2)
            runner.cancel()  # e.g. Ctrl+C while _interrupt waits for the turn
            done, _ = await asyncio.wait({runner}, timeout=2)
            assert runner in done, "run() swallowed its own cancellation"
            assert runner.cancelled()
            assert parts["mic"].starts == 1  # never started listening for the new turn
        finally:
            # On failure, don't hang asyncio.run's teardown: let the turn finish, then
            # cancel the runner where it is (a swallowing runner would eat that cancel too).
            chat.hold.set()
            await asyncio.wait({turn})
            runner.cancel()
            await asyncio.wait({runner}, timeout=2)

    asyncio.run(scenario())


def test_run_survives_handler_error():
    async def scenario():
        idle = asyncio.Event()
        states: list[State] = []

        def on_state(state: State) -> None:
            states.append(state)
            if state is State.IDLE:
                idle.set()

        speaker = FakeSpeaker()
        orch = Orchestrator(
            mic=FlakyMic(),
            transcriber=FakeTranscriber(),
            chat=FakeChat(),
            synthesizer=FakeSynth(),
            speaker=speaker,
            on_state=on_state,
        )
        events: asyncio.Queue[str] = asyncio.Queue()
        runner = asyncio.create_task(orch.run(events))
        for event in ("down", "down", "up"):  # first down: mic.start() raises
            events.put_nowait(event)
        done, _ = await asyncio.wait(
            {runner, asyncio.create_task(idle.wait())},
            timeout=2,
            return_when=asyncio.FIRST_COMPLETED,
        )
        assert runner not in done, "run() died on a handler error"
        assert idle.is_set()
        runner.cancel()
        assert speaker.played == ["Hello there.", "How are you?"]

    asyncio.run(scenario())


def test_reply_log_includes_canned_line(caplog):
    caplog.set_level(logging.INFO, logger="desk_buddy.loop")
    orch, _, _ = make(chat=FakeChat(deltas=("First. ",), error=LLMError("boom")))
    asyncio.run(talk(orch))
    assert f"CARL [neutral]: First. {ERROR_LINE.text}" in caplog.messages


def test_canned_line_log_format(caplog):
    caplog.set_level(logging.INFO, logger="desk_buddy.loop")
    orch, _, _ = make(transcriber=FakeTranscriber(text="Forget that."))
    asyncio.run(talk(orch))
    assert f"CARL [{FORGET_LINE.emotion}]: {FORGET_LINE.text}" in caplog.messages


def test_second_turn_sees_first_turn_in_history():
    orch, parts, _ = make()

    async def scenario():
        await talk(orch)
        await talk(orch)

    asyncio.run(scenario())
    assert parts["chat"].histories[0] == []
    assert parts["chat"].histories[1] == [
        {"role": "user", "content": "hi buddy"},
        {"role": "assistant", "content": "[neutral] Hello there. How are you?"},
    ]


def test_emotion_tag_is_reported_stripped_and_remembered():
    orch, parts, _ = make(chat=FakeChat(deltas=("[hap", "py] Hi", " there.")))
    asyncio.run(talk(orch))
    assert parts["emotions"] == ["happy"]
    assert parts["speaker"].played == ["Hi there."]
    assert parts["conversation"].messages()[1]["content"] == "[happy] Hi there."


def test_interrupt_remembers_only_what_started_playing():
    speaker = FakeSpeaker(block=True)
    orch, parts, _ = make(speaker=speaker)

    async def scenario():
        await orch.handle("down")
        await orch.handle("up")
        assert await asyncio.to_thread(speaker.started.wait, 2)
        await orch.handle("down")  # interrupt during "Hello there."
        speaker.block = False
        await orch.handle("up")  # next question
        await orch.wait_idle()

    asyncio.run(scenario())
    conversation = parts["conversation"]
    assert len(conversation) == 2
    # CARL's stored reply stays clean (Claude copies markers it sees in its own replies)...
    assert conversation.messages()[1]["content"] == "[neutral] Hello there."
    # ...and the next user message carries the note instead.
    assert parts["chat"].prompts[1] == f"{INTERRUPTED_NOTE} hi buddy"


class SlowChat:
    """Never yields; records that it was called."""

    def __init__(self):
        self.called = asyncio.Event()

    async def stream_reply(self, history, text):
        self.called.set()
        await asyncio.Event().wait()
        yield ""  # pragma: no cover


def test_interrupt_before_any_speech_remembers_nothing():
    async def scenario():
        chat = SlowChat()
        orch, parts, _ = make(chat=chat)
        await orch.handle("down")
        await orch.handle("up")
        await asyncio.wait_for(chat.called.wait(), 2)
        await orch.handle("down")  # interrupt while thinking
        return parts

    parts = asyncio.run(scenario())
    assert len(parts["conversation"]) == 0


def test_failed_turn_is_not_remembered_and_looks_sad():
    orch, parts, _ = make(chat=FakeChat(deltas=("First. ",), error=LLMError("boom")))
    asyncio.run(talk(orch))
    assert len(parts["conversation"]) == 0
    assert parts["emotions"] == ["neutral", "sad"]


def test_refusal_is_not_remembered():
    orch, parts, _ = make(chat=FakeChat(deltas=(), error=LLMRefusal("no")))
    asyncio.run(talk(orch))
    assert len(parts["conversation"]) == 0


def test_forget_phrase_clears_memory_without_calling_claude():
    conversation = Conversation()
    conversation.add("tell me about labs", "[happy] Labs are great.")
    orch, parts, states = make(
        transcriber=FakeTranscriber(text="Buddy, new topic."), conversation=conversation
    )
    asyncio.run(talk(orch))
    assert parts["chat"].prompts == []
    assert len(conversation) == 0
    assert parts["speaker"].played == [FORGET_LINE.text]
    assert parts["emotions"] == [FORGET_LINE.emotion]
    assert states == [State.LISTENING, State.THINKING, State.SPEAKING, State.IDLE]


def test_reset_interrupts_a_turn_and_goes_idle():
    speaker = FakeSpeaker(block=True)
    orch, _, states = make(speaker=speaker)

    async def scenario():
        await orch.handle("down")
        await orch.handle("up")
        assert await asyncio.to_thread(speaker.started.wait, 2)
        await orch.handle("reset")

    asyncio.run(scenario())
    assert speaker.stops == 1
    assert orch.state is State.IDLE
    assert states == [State.LISTENING, State.THINKING, State.SPEAKING, State.IDLE]


def test_reset_while_listening_stops_the_mic():
    mic = FakeMic()
    orch, _, states = make(mic=mic)

    async def scenario():
        await orch.handle("down")
        await orch.handle("reset")

    asyncio.run(scenario())
    assert mic.stops == 1
    assert states == [State.LISTENING, State.IDLE]


def test_reset_when_idle_does_nothing():
    mic = FakeMic()
    orch, parts, states = make(mic=mic)
    asyncio.run(orch.handle("reset"))
    assert mic.stops == 0
    assert parts["speaker"].stops == 0
    assert states == []


def test_reset_goes_idle_even_if_the_mic_fails_to_stop():
    class BrokenStopMic(FakeMic):
        def stop(self):
            super().stop()
            raise OSError("device gone")

    orch, _, states = make(mic=BrokenStopMic())

    async def scenario():
        await orch.handle("down")
        with pytest.raises(OSError):
            await orch.handle("reset")

    asyncio.run(scenario())
    assert orch.state is State.IDLE
    assert states == [State.LISTENING, State.IDLE]


def test_a_fresh_turn_after_reset_works_with_a_new_token():
    speaker = FakeSpeaker(block=True)
    orch, _, states = make(speaker=speaker)

    async def scenario():
        await orch.handle("down")
        await orch.handle("up")
        assert await asyncio.to_thread(speaker.started.wait, 2)
        await orch.handle("reset")
        speaker.block = False
        speaker.played.clear()
        speaker.tokens.clear()
        await talk(orch)

    asyncio.run(scenario())
    assert speaker.played == ["Hello there.", "How are you?"]
    assert speaker.tokens == [1, 1]
    assert orch.state is State.IDLE
    assert states[-4:] == [State.LISTENING, State.THINKING, State.SPEAKING, State.IDLE]


# --- Signature lines: greeting and farewell ---


class FakeClock:
    def __init__(self, now: float = 0.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def test_say_speaks_a_canned_line_with_its_emotion_and_does_not_remember_it():
    orch, parts, states = make()

    async def scenario():
        assert orch.say(GREETING)
        await orch.wait_idle()

    asyncio.run(scenario())
    assert parts["speaker"].played == [GREETING.text]
    assert parts["emotions"] == [GREETING.emotion]
    assert states == [State.SPEAKING, State.IDLE]
    assert len(orch.conversation) == 0


def test_say_is_skipped_unless_idle():
    orch, parts, _ = make()

    async def scenario():
        await orch.handle("down")
        assert not orch.say(GREETING)

    asyncio.run(scenario())
    assert parts["speaker"].played == []


def test_a_key_press_interrupts_a_canned_line():
    speaker = FakeSpeaker(block=True)
    orch, _, states = make(speaker=speaker)

    async def scenario():
        orch.say(GREETING)
        assert await asyncio.to_thread(speaker.started.wait, 2)
        await orch.handle("down")

    asyncio.run(scenario())
    assert speaker.stops == 1
    assert orch.state is State.LISTENING


def test_greet_event_greets_at_start_then_dedupes_within_a_minute():
    clock = FakeClock(0.0)
    orch, parts, _ = make(clock=clock)

    async def greet_at(t):
        clock.now = t
        await orch.handle("greet")
        await orch.wait_idle()

    async def scenario():
        await greet_at(0.0)  # startup
        await greet_at(10.0)  # a robot connects right after: no double greeting
        assert parts["speaker"].played == [GREETING.text]
        await greet_at(70.0)  # a robot connects later: greeted
        assert parts["speaker"].played == [GREETING.text, GREETING.text]

    asyncio.run(scenario())


def test_a_skipped_greeting_does_not_count():
    clock = FakeClock(0.0)
    orch, parts, _ = make(clock=clock)

    async def scenario():
        await orch.handle("down")
        await orch.handle("greet")  # busy listening: skipped
        await orch.handle("reset")
        clock.now = 5.0
        await orch.handle("greet")
        await orch.wait_idle()

    asyncio.run(scenario())
    assert parts["speaker"].played == [GREETING.text]


def test_farewell_interrupts_a_turn_and_says_goodbye():
    speaker = FakeSpeaker(block=True)
    orch, _, states = make(speaker=speaker)

    async def scenario():
        await orch.handle("down")
        await orch.handle("up")
        assert await asyncio.to_thread(speaker.started.wait, 2)
        speaker.block = False
        await orch.farewell(timeout=2)

    asyncio.run(scenario())
    assert speaker.played[-1] == FAREWELL.text
    assert orch.state is State.IDLE


def test_farewell_while_listening_stops_the_mic_first():
    orch, parts, _ = make()

    async def scenario():
        await orch.handle("down")
        await orch.farewell(timeout=2)

    asyncio.run(scenario())
    assert parts["mic"].stops == 1
    assert parts["speaker"].played == [FAREWELL.text]


def test_farewell_is_bounded_and_stops_the_speaker_on_timeout():
    speaker = FakeSpeaker(block=True)  # plays until stopped
    orch, _, _ = make(speaker=speaker)

    async def scenario():
        started = time.monotonic()
        await orch.farewell(timeout=0.1)
        return time.monotonic() - started

    assert asyncio.run(scenario()) < 1
    assert speaker.played == [FAREWELL.text]
    assert speaker.stops == 1  # cut off, not left playing into shutdown


def test_farewell_never_raises_if_speaking_fails():
    class BrokenSynth:
        def synthesize(self, text):
            raise OSError("no voice")

    orch, _, _ = make(synthesizer=BrokenSynth())
    asyncio.run(orch.farewell(timeout=1))
    assert orch.state is State.IDLE


# --- Signature lines: pondering and task done ---


class DelayedChat:
    """Waits `delay` seconds, then replies. `tools` successful tool calls are counted
    the way ClaudeChat counts them."""

    def __init__(self, delay=0.0, deltas=("[happy] Done. ",), tools=0, error=None):
        self.delay, self.deltas, self.tools, self.error = delay, deltas, tools, error
        self.tools_succeeded = 0

    async def stream_reply(self, history, text):
        await asyncio.sleep(self.delay)
        self.tools_succeeded += self.tools
        for d in self.deltas:
            yield d
        if self.error:
            raise self.error


@pytest.fixture
def quick_ponder(monkeypatch):
    """Ponder after 0.2 s instead of 4 s, so the tests stay fast."""
    monkeypatch.setattr(loop_module, "PONDER_AFTER", 0.2)


def remembered(orch):
    return " ".join(m["content"] for m in orch.conversation.messages())


def test_pondering_fires_for_a_slow_reply_and_not_twice_within_five_minutes(quick_ponder, caplog):
    caplog.set_level(logging.INFO, logger="desk_buddy.loop")
    clock = FakeClock(1000.0)
    orch, parts, _ = make(chat=DelayedChat(delay=0.6), clock=clock)

    async def scenario():
        await talk(orch)
        assert parts["speaker"].played == [PONDERING.text, "Done."]
        clock.now += loop_module.PONDER_EVERY - 1
        await talk(orch)  # slow again, but pondered too recently
        assert parts["speaker"].played[2:] == ["Done."]
        clock.now += 2
        await talk(orch)
        assert parts["speaker"].played[3:] == [PONDERING.text, "Done."]

    asyncio.run(scenario())
    assert parts["emotions"][:2] == ["thinking", "happy"]
    assert f"CARL [thinking]: {PONDERING.text}" in caplog.messages
    assert PONDERING.text not in remembered(orch)


def test_pondering_does_not_fire_if_audio_starts_in_time(monkeypatch):
    monkeypatch.setattr(loop_module, "PONDER_AFTER", 0.5)
    orch, parts, _ = make(chat=DelayedChat(delay=0.1))
    asyncio.run(talk(orch))
    assert parts["speaker"].played == ["Done."]


def test_an_interrupt_during_pondering_cancels_the_reply(quick_ponder):
    speaker = FakeSpeaker(block=True)
    orch, _, _ = make(chat=DelayedChat(delay=0.4), speaker=speaker)

    async def scenario():
        await orch.handle("down")
        await orch.handle("up")
        assert await asyncio.to_thread(speaker.started.wait, 2)
        assert speaker.played == [PONDERING.text]
        await orch.handle("down")  # interrupt: the turn is cancelled and awaited

    asyncio.run(scenario())
    assert speaker.played == [PONDERING.text]
    assert orch.state is State.LISTENING
    assert len(orch.conversation) == 0


@pytest.mark.parametrize(
    ("tools", "roll", "said"),
    [(1, 0.2, True), (2, 0.0, True), (1, 0.25, False), (0, 0.0, False)],
)
def test_task_done_after_a_successful_tool_when_the_dice_say_so(tools, roll, said):
    orch, parts, _ = make(chat=DelayedChat(tools=tools), random=lambda: roll)
    asyncio.run(talk(orch))
    expected = ["Done.", TASK_DONE.text] if said else ["Done."]
    assert parts["speaker"].played == expected
    assert TASK_DONE.text not in remembered(orch)
    assert remembered(orch).endswith("[happy] Done.")


def test_no_task_done_after_a_failed_reply():
    chat = DelayedChat(tools=1, error=LLMError("boom"))
    orch, parts, _ = make(chat=chat, random=lambda: 0.0)
    asyncio.run(talk(orch))
    assert TASK_DONE.text not in parts["speaker"].played


def test_a_chat_without_a_tool_count_never_says_task_done():
    orch, parts, _ = make(random=lambda: 0.0)  # FakeChat has no tools_succeeded
    asyncio.run(talk(orch))
    assert parts["speaker"].played == ["Hello there.", "How are you?"]
