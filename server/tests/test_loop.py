import asyncio
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

    async def stream_reply(self, text: str):
        self.prompts.append(text)
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
