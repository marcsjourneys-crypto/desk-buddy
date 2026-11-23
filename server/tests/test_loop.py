import asyncio
import logging
import threading

from desk_buddy.audio import BYTES_PER_SECOND
from desk_buddy.llm import LLMError, LLMRefusal
from desk_buddy.loop import CANNED_ERROR, CANNED_REFUSAL, Orchestrator, State

ONE_SECOND = b"\x00" * BYTES_PER_SECOND


class FakeMic:
    def __init__(self, pcm: bytes = ONE_SECOND):
        self.pcm, self.starts = pcm, 0

    def start(self):
        self.starts += 1

    def stop(self) -> bytes:
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
    }
    parts.update(overrides)
    states: list[State] = []
    orch = Orchestrator(**parts, on_state=states.append)
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
    assert parts["speaker"].played == [CANNED_ERROR]
    assert states[-1] is State.IDLE


def test_error_after_partial_reply_keeps_spoken_part():
    orch, parts, _ = make(chat=FakeChat(deltas=("First. ",), error=LLMError("boom")))
    asyncio.run(talk(orch))
    assert parts["speaker"].played == ["First.", CANNED_ERROR]


def test_refusal_speaks_refusal_line():
    orch, parts, _ = make(chat=FakeChat(deltas=(), error=LLMRefusal("no")))
    asyncio.run(talk(orch))
    assert parts["speaker"].played == [CANNED_REFUSAL]


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
    assert f"Buddy: First. {CANNED_ERROR}" in caplog.messages
