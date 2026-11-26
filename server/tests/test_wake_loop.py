"""The wake word through the whole server: the fake robot, the real link, the real loop
(fake speech-to-text, Claude and voice) and the real end-of-speech model, fed the Piper
speech fixture (see tests/fixtures/README.md)."""

import asyncio
import contextlib
import logging
import wave
from pathlib import Path

import pytest

from desk_buddy.audio import BYTES_PER_SECOND
from desk_buddy.brain.memory import Conversation
from desk_buddy.events import EventBus
from desk_buddy.fake_robot import FakeRobot
from desk_buddy.link.switch import DeviceSwitch
from desk_buddy.loop import HandsFree, Orchestrator, State, strip_wake_phrase
from desk_buddy.server import create_app, start_server
from desk_buddy.tools.actions import ActionGuard
from desk_buddy.tools.confirm import Confirmations
from desk_buddy.tools.launch import LaunchTools
from desk_buddy.tools.urls import SearchResults

TOKEN = "t" * 20
FIXTURE = Path(__file__).parent / "fixtures" / "speech.wav"
IDLE = {"type": "state", "name": "idle"}
LISTENING = {"type": "state", "name": "listening"}
THINKING = {"type": "state", "name": "thinking"}
SPEAKING = {"type": "state", "name": "speaking"}
QUICK = HandsFree(follow_up_delay=0.05)  # the real timings, but a short gap


def speech() -> bytes:
    with wave.open(str(FIXTURE), "rb") as w:
        return w.readframes(w.getnframes())


def silence(seconds: float) -> bytes:
    return bytes(round(seconds * BYTES_PER_SECOND) & ~1)


QUESTION = speech() + silence(1.2)  # speech, then enough silence to end it


class NullMic:
    def start(self):
        pass

    def stop(self):
        return b""


class NullPlayer:
    def current_token(self):
        return 0

    def play(self, pcm, token):
        pass

    def stop(self):
        pass


class Transcriber:
    def __init__(self, words=("What's the weather like?",)):
        self.words = list(words)
        self.heard = []  # seconds of audio per call

    def transcribe(self, pcm):
        self.heard.append(len(pcm) / BYTES_PER_SECOND)
        return self.words.pop(0) if len(self.words) > 1 else self.words[0]


class Chat:
    def __init__(self):
        self.prompts = []

    async def stream_reply(self, history, text):
        self.prompts.append(text)
        yield "[happy] Sunny all day."


class Synth:
    def synthesize(self, text):
        return b"\x01\x00" * (BYTES_PER_SECOND // 20)  # 0.1 s


class BlockingSynth(Synth):
    """Makes long replies (3 s), so a test can act while CARL is speaking."""

    def synthesize(self, text):
        return b"\x01\x00" * (3 * BYTES_PER_SECOND // 2)


class Rig:
    def __init__(self, hands_free=QUICK, chat=None, synth=None, transcriber=None, **extra):
        self.bus = EventBus()
        self.switch = DeviceSwitch(NullMic(), NullPlayer())
        self.events: asyncio.Queue[str] = asyncio.Queue()
        self.transcriber = transcriber or Transcriber()
        self.chat = chat or Chat()
        self.orch = Orchestrator(
            mic=self.switch.mic,
            transcriber=self.transcriber,
            chat=self.chat,
            synthesizer=synth or Synth(),
            speaker=self.switch.player,
            conversation=Conversation(),
            on_state=self.switch.send_state,
            on_emotion=self.switch.send_emotion,
            bus=self.bus,
            hands_free=lambda: hands_free,
            **extra,
        )

    def of(self, kind):
        return [e for e in self.bus.recent() if e["type"] == kind]

    @contextlib.asynccontextmanager
    async def running(self):
        app = create_app(
            token=TOKEN,
            switch=self.switch,
            events=self.events,
            current_state=lambda: self.orch.state.value,
            on_wake=lambda score: self.events.put_nowait(f"wake {score}"),
        )
        server = await start_server(app, "127.0.0.1", 0)
        loop_task = asyncio.create_task(self.orch.run(self.events))
        robot = FakeRobot(f"ws://127.0.0.1:{server.port}/robot", TOKEN)
        try:
            await robot.connect()
            await robot.recv_until("state")
            yield robot
        finally:
            await robot.close()
            loop_task.cancel()
            await self.orch.shutdown()
            await asyncio.wait_for(server.stop(), 5)


async def until(robot, wanted, timeout=10.0):
    """Text frames up to and including the first one equal to `wanted`."""
    frames = []
    async with asyncio.timeout(timeout):
        while True:
            frame = await robot.recv(timeout=None)
            assert frame is not None, f"closed before {wanted}"
            if isinstance(frame, dict):
                frames.append(frame)
                if frame == wanted:
                    return frames


def states(frames):
    return [f["name"] for f in frames if f["type"] == "state"]


async def poll(condition, timeout=5.0):
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)


def test_wake_question_reply_follow_up_question_reply_then_idle():
    rig = Rig(transcriber=Transcriber(["What's the weather like?", "And tomorrow?"]))

    async def run():
        async with rig.running() as robot:
            await robot.wake_and_stream(QUESTION, score=0.98)
            first = await until(robot, LISTENING)  # the reply, then the follow-up
            await robot.send_audio(QUESTION)
            second = await until(robot, LISTENING)
            await robot.send_audio(silence(6))  # nobody speaks: the window closes
            last = await until(robot, IDLE)
            return first, second, last

    first, second, last = asyncio.run(run())
    # No idle between the reply and the follow-up: the eyes stay awake.
    assert states(first) == ["thinking", "speaking", "listening"]
    assert states(second) == ["thinking", "speaking", "listening"]
    assert states(last) == ["idle"]
    assert rig.chat.prompts == ["What's the weather like?", "And tomorrow?"]
    # Each clip is the speech plus 0.8 s of silence, give or take a few chunks.
    assert all(4.0 < seconds < 4.8 for seconds in rig.transcriber.heard)
    assert [e["score"] for e in rig.of("wake")] == [0.98]
    assert [(e["active"], e.get("heard")) for e in rig.of("follow_up")] == [
        (True, None),
        (False, True),
        (True, None),
        (False, False),
    ]
    assert rig.of("false_wake") == []
    assert rig.orch.state is State.IDLE


def test_a_false_wake_goes_back_to_idle(caplog):
    caplog.set_level(logging.INFO)
    rig = Rig()

    async def run():
        async with rig.running() as robot:
            await robot.wake_and_stream(silence(5))
            return await until(robot, IDLE)

    assert states(asyncio.run(run())) == ["idle"]
    assert rig.transcriber.heard == [] and rig.chat.prompts == []
    assert len(rig.of("false_wake")) == 1
    assert "False wake: no speech" in caplog.messages


def test_a_click_after_a_wake_is_a_false_wake_not_a_question():
    rig = Rig()
    start = BYTES_PER_SECOND  # 0.5 s into the fixture
    click = speech()[start : start + BYTES_PER_SECOND // 5]  # 0.2 s

    async def run():
        async with rig.running() as robot:
            await robot.wake_and_stream(silence(1) + click + silence(5))
            await until(robot, IDLE)

    asyncio.run(run())
    assert rig.transcriber.heard == []
    assert len(rig.of("false_wake")) == 1


def test_a_long_question_is_cut_off_at_15_seconds(caplog):
    caplog.set_level(logging.INFO)
    rig = Rig(hands_free=HandsFree(follow_up_seconds=0))

    async def run():
        async with rig.running() as robot:
            await robot.wake_and_stream(speech() * 6)  # 22 s without a long pause
            return await until(robot, IDLE)

    frames = asyncio.run(run())
    assert states(frames) == ["thinking", "speaking", "idle"]
    assert len(rig.transcriber.heard) == 1
    assert 15.0 <= rig.transcriber.heard[0] < 15.1  # cut at the chunk that hit the cap
    assert "Question cut off at the 15 s cap" in caplog.messages


def test_the_key_takes_over_a_wake_recording():
    rig = Rig()

    async def run():
        async with rig.running() as robot:
            await robot.wake_and_stream(silence(0.5))
            await poll(lambda: rig.switch.robot.mic.on_frame is not None)
            await robot.press()  # like the PC key: push-to-talk from here
            await robot.send_audio(silence(5))  # 5 s of silence: no false wake now
            await robot.send_audio(b"\x05\x00" * (BYTES_PER_SECOND // 4))
            await robot.release()
            return await until(robot, IDLE)

    frames = asyncio.run(run())
    assert states(frames) == ["thinking", "speaking", "idle"]  # no follow-up: key-started
    assert rig.of("false_wake") == []
    assert rig.switch.robot is None or rig.switch.robot.mic.on_frame is None
    # Only the audio after the press: 5 s of silence and 0.5 s.
    assert rig.transcriber.heard == [5.5]


def test_the_key_takes_over_a_follow_up():
    rig = Rig()

    async def run():
        async with rig.running() as robot:
            await robot.wake_and_stream(QUESTION)
            await until(robot, LISTENING)  # the follow-up
            await robot.press()
            await robot.send_audio(silence(6))  # longer than the window
            await robot.release()
            frames = await until(robot, IDLE)
            return frames

    frames = asyncio.run(run())
    assert states(frames) == ["thinking", "speaking", "idle"]
    assert len(rig.transcriber.heard) == 2 and rig.transcriber.heard[1] == 6.0
    assert [(e["active"], e.get("heard")) for e in rig.of("follow_up")] == [
        (True, None),
        (False, False),
    ]


def test_a_wake_while_speaking_is_ignored_and_the_key_still_interrupts(caplog):
    caplog.set_level(logging.DEBUG, logger="desk_buddy.loop")
    rig = Rig(synth=BlockingSynth())

    async def run():
        async with rig.running() as robot:
            await robot.wake_and_stream(QUESTION)
            await until(robot, SPEAKING)
            await asyncio.sleep(2.1)  # past the link's wake limit
            await robot.wake(0.99)
            await poll(lambda: any("Ignored a wake" in m for m in caplog.messages))
            assert rig.orch.state is State.SPEAKING
            await robot.press()  # the key interrupts as today
            frames = await until(robot, LISTENING)
            await robot.release()
            await until(robot, IDLE)
            return frames

    frames = asyncio.run(run())
    assert {"type": "audio_stop"} in frames
    assert len(rig.of("wake")) == 1
    assert rig.of("follow_up") == []  # an interrupted reply gets no follow-up


class BrokenEndpointer:
    def __init__(self, **kwargs):
        pass

    def feed(self, pcm):
        raise RuntimeError("onnxruntime fell over")


def test_an_endpointer_error_ends_the_recording_safely(caplog):
    rig = Rig(endpointer=BrokenEndpointer)

    async def run():
        async with rig.running() as robot:
            await robot.wake_and_stream(QUESTION)
            await until(robot, IDLE)
            # Still working: the key works, and the next wake is tried again.
            await robot.press()
            await robot.send_audio(silence(0.5))
            await robot.release()
            await until(robot, IDLE)

    asyncio.run(run())
    assert "End-of-speech detection failed" in caplog.messages
    assert "Hands-free listening stopped: end-of-speech detection failed" in caplog.messages
    assert rig.transcriber.heard == [0.5]


def test_follow_up_off_goes_idle_after_the_reply():
    rig = Rig(hands_free=HandsFree(follow_up_seconds=0))

    async def run():
        async with rig.running() as robot:
            await robot.wake_and_stream(QUESTION)
            return await until(robot, IDLE)

    assert states(asyncio.run(run())) == ["thinking", "speaking", "idle"]
    assert rig.of("follow_up") == []


# --- a spoken yes in the follow-up window ---

PAGE = "https://www.bbc.co.uk/news/articles/c123"
ASK = "Shall I open the page on bbc.co.uk?"


class ProposingChat(Chat):
    """Calls open_search_result from a tool thread (as ClaudeChat does), then replies."""

    def __init__(self, tools):
        super().__init__()
        self.tools = tools

    async def stream_reply(self, history, text):
        self.prompts.append(text)
        if len(self.prompts) == 1:
            await asyncio.to_thread(self.tools.open_search_result, "1")
            yield "[happy] I can open that page."
        else:
            yield "[happy] It's open."


def test_a_yes_in_the_follow_up_window_opens_the_page():
    bus = EventBus()
    guard = ActionGuard(True, bus=bus)
    results = SearchResults()
    results.add([PAGE])
    confirmations = Confirmations(bus=bus)
    opened = []
    tools = LaunchTools(guard, results=results, confirmations=confirmations, opener=opened.append)
    rig = Rig(
        chat=ProposingChat(tools),
        transcriber=Transcriber(["Open the first one.", "Yes please."]),
        confirmations=confirmations,
        search_results=results,
        on_question=guard.new_question,
    )

    async def run():
        async with rig.running() as robot:
            await robot.wake_and_stream(QUESTION)
            await until(robot, LISTENING)  # after the reply and the question
            assert confirmations.pending is not None and confirmations.pending.armed
            await robot.send_audio(QUESTION)  # "Yes please."
            await until(robot, LISTENING)

    asyncio.run(run())
    assert opened == [PAGE]
    assert rig.chat.prompts[1].startswith("Yes please. (System note: the server asked")


def test_a_wake_with_no_robot_owning_the_mic_is_ignored():
    rig = Rig()  # never connected: the switch uses the PC mic

    async def run():
        await rig.orch.handle("wake 0.99")

    asyncio.run(run())
    assert rig.orch.state is State.IDLE
    assert rig.of("wake") == []


def test_the_robot_mic_tap_sees_recorded_frames_and_a_new_press_drops_it():
    from desk_buddy.link.robot import RobotMic

    mic, seen = RobotMic(), []
    mic.feed(b"\x01\x00")  # not recording: not seen
    mic.begin()
    mic.on_frame = seen.append
    mic.feed(b"\x02\x00")
    mic.begin()  # a key press: the tap is dropped at once
    mic.feed(b"\x03\x00")
    assert seen == [b"\x02\x00"]
    assert mic.stop() == b"\x03\x00"


def test_a_failing_tap_never_loses_the_frame():
    from desk_buddy.link.robot import RobotMic

    def broken(pcm):
        raise RuntimeError("boom")

    mic = RobotMic()
    mic.begin()
    mic.on_frame = broken
    mic.feed(b"\x02\x00")
    assert mic.stop() == b"\x02\x00"


# --- the end of "CARL" in the robot's pre-roll ---

# 0.3 s of speech: alone, enough to start a question (measured: SPEECH_START at 0.26 s).
TAIL = speech()[3 * BYTES_PER_SECOND // 2 : 3 * BYTES_PER_SECOND // 2 + 3 * BYTES_PER_SECOND // 10]


def test_speech_only_in_the_pre_roll_is_a_false_wake():
    rig = Rig()

    async def run():
        async with rig.running() as robot:
            await robot.wake_and_stream(TAIL + silence(5))
            await until(robot, IDLE)

    asyncio.run(run())
    assert rig.transcriber.heard == []
    assert len(rig.of("false_wake")) == 1


def test_without_ignore_first_the_same_pre_roll_would_be_a_question():
    rig = Rig(hands_free=HandsFree(follow_up_seconds=0, ignore_first=0.0))

    async def run():
        async with rig.running() as robot:
            await robot.wake_and_stream(TAIL + silence(5))
            await until(robot, IDLE)

    asyncio.run(run())
    assert len(rig.transcriber.heard) == 1


def test_a_transcript_of_only_the_wake_phrase_is_a_false_wake(caplog):
    caplog.set_level(logging.INFO)
    rig = Rig(transcriber=Transcriber(["Hey, Carl."]))

    async def run():
        async with rig.running() as robot:
            await robot.wake_and_stream(QUESTION)
            return await until(robot, IDLE)

    assert states(asyncio.run(run())) == ["thinking", "idle"]  # no reply, no follow-up
    assert rig.chat.prompts == []
    assert len(rig.of("false_wake")) == 1
    assert rig.of("follow_up") == []
    assert "False wake: heard only 'Hey, Carl.'" in caplog.messages


def test_the_wake_phrase_is_stripped_from_a_wake_question_but_not_a_follow_up():
    rig = Rig(transcriber=Transcriber(["Carl, what's the weather?", "Carl is a good name."]))

    async def run():
        async with rig.running() as robot:
            await robot.wake_and_stream(QUESTION)
            await until(robot, LISTENING)
            await robot.send_audio(QUESTION)
            await until(robot, LISTENING)

    asyncio.run(run())
    assert rig.chat.prompts == ["what's the weather?", "Carl is a good name."]
    assert [e["text"] for e in rig.bus.recent() if e["type"] == "heard"] == rig.chat.prompts


@pytest.mark.parametrize(
    ("heard", "question"),
    [
        ("Hey Carl, what time is it?", "what time is it?"),
        ("hey karl what time is it", "what time is it"),
        ("CARL. What time is it?", "What time is it?"),
        ("Karl", ""),
        ("Hey, Carl!", ""),
        ("", ""),
        ("Carl's book is on the shelf.", "Carl's book is on the shelf."),
        ("Carlos called.", "Carlos called."),
        ("What time is it, Carl?", "What time is it, Carl?"),
    ],
)
def test_strip_wake_phrase(heard, question):
    assert strip_wake_phrase(heard) == question
