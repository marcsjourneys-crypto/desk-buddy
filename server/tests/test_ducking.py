"""Music ducking: Spotify drops to 20 % while CARL listens, thinks and talks, and comes
back when the turn ends, whatever ends it. Best effort, never on the event loop."""

import asyncio
import threading
import time

import pytest
from spotipy.exceptions import SpotifyException

from desk_buddy.audio import BYTES_PER_SECOND
from desk_buddy.llm import LLMError
from desk_buddy.loop import Orchestrator, State
from desk_buddy.tools.actions import ActionGuard
from desk_buddy.tools.ducking import MusicDucker
from desk_buddy.tools.registry import ToolRegistry
from desk_buddy.tools.spotify import MusicTools, SpotifyControl


class FakeSpotify:
    def __init__(self, volume=80, is_playing=True, fail=False, delay=0.0):
        self.volume_percent, self.is_playing = volume, is_playing
        self.fail, self.delay = fail, delay
        self.volumes = []
        self.threads = set()

    def current_playback(self, market=None, additional_types=None):
        self.threads.add(threading.current_thread().name)
        time.sleep(self.delay)
        if self.fail:
            raise SpotifyException(502, -1, "Bad gateway")
        return {
            "is_playing": self.is_playing,
            "device": {"id": "pc", "volume_percent": self.volume_percent},
        }

    def volume(self, volume_percent, device_id=None):
        self.threads.add(threading.current_thread().name)
        self.volumes.append(volume_percent)
        self.volume_percent = volume_percent


def ducker_for(client):
    return MusicDucker(SpotifyControl(client, pc_name="pc"))


def settle(ducker):
    assert ducker.flush(timeout=2)


# --- the ducker on its own ---


def test_ducks_to_a_fifth_and_restores_the_level():
    client = FakeSpotify(volume=80)
    ducker = ducker_for(client)
    ducker.on_state(State.LISTENING)
    settle(ducker)
    assert client.volumes == [16]
    ducker.on_state(State.THINKING)  # still ducked: nothing more
    ducker.on_state(State.SPEAKING)
    ducker.on_state(State.IDLE)
    settle(ducker)
    assert client.volumes == [16, 80]
    ducker.close()


def test_does_nothing_when_music_isnt_playing():
    client = FakeSpotify(is_playing=False)
    ducker = ducker_for(client)
    ducker.on_state(State.LISTENING)
    ducker.on_state(State.IDLE)
    settle(ducker)
    assert client.volumes == []
    ducker.close()


def test_spotify_calls_never_run_on_the_calling_thread():
    client = FakeSpotify(delay=0.3)
    ducker = ducker_for(client)
    started = time.perf_counter()
    ducker.on_state(State.LISTENING)  # returns at once: listening is never delayed
    assert time.perf_counter() - started < 0.05
    settle(ducker)
    assert threading.current_thread().name not in client.threads
    ducker.close()


def test_a_failing_duck_is_only_a_warning(caplog):
    client = FakeSpotify(fail=True)
    ducker = ducker_for(client)
    ducker.on_state(State.LISTENING)
    ducker.on_state(State.IDLE)
    settle(ducker)
    assert client.volumes == []
    assert "Music duck failed" in caplog.text
    ducker.close()


def test_a_failed_restore_is_tried_again_after_the_next_turn():
    client = FakeSpotify(volume=50)
    ducker = ducker_for(client)
    ducker.on_state(State.LISTENING)
    settle(ducker)

    def broken(volume_percent, device_id=None):
        raise SpotifyException(502, -1, "Bad gateway")

    good = client.volume
    client.volume = broken
    ducker.on_state(State.IDLE)  # the restore fails
    settle(ducker)
    client.volume = good
    ducker.on_state(State.LISTENING)  # already ducked: no second duck (that would lose 50)
    ducker.on_state(State.IDLE)
    settle(ducker)
    assert client.volumes == [10, 50]
    ducker.close()


def test_a_volume_set_by_the_user_while_ducked_is_not_undone():
    client = FakeSpotify(volume=80)
    control = SpotifyControl(client, pc_name="pc")
    ducker = MusicDucker(control)
    tools = ToolRegistry(MusicTools(control, ActionGuard(True), ducker=ducker).tools())
    ducker.on_state(State.LISTENING)
    settle(ducker)
    out = tools.run("music_volume", {"level": None, "change": -20})  # from 80, not from 16
    assert not out.is_error
    ducker.on_state(State.IDLE)
    settle(ducker)
    assert client.volumes == [16, 60]  # no restore to 80 afterwards
    ducker.close()


def test_close_waits_for_a_pending_restore():
    client = FakeSpotify(volume=40, delay=0.1)
    ducker = ducker_for(client)
    ducker.on_state(State.LISTENING)
    ducker.on_state(State.IDLE)
    ducker.close()
    assert client.volumes == [8, 40]


# --- through a real Orchestrator: every way a turn ends restores ---

ONE_SECOND = b"\x00" * BYTES_PER_SECOND


class Mic:
    def start(self):
        pass

    def stop(self):
        return ONE_SECOND


class Transcriber:
    def transcribe(self, pcm):
        return "hello"


class Chat:
    def __init__(self, error=None, hold=None):
        self.error, self.hold = error, hold

    async def stream_reply(self, history, text):
        if self.hold:
            await self.hold.wait()
        yield "[happy] Hi there. "
        if self.error:
            raise self.error


class Synth:
    def synthesize(self, text):
        return text.encode()


class Speaker:
    def current_token(self):
        return 0

    def play(self, pcm, token):
        pass

    def stop(self):
        pass


def orchestrator(ducker, chat=None):
    return Orchestrator(
        mic=Mic(),
        transcriber=Transcriber(),
        chat=chat or Chat(),
        synthesizer=Synth(),
        speaker=Speaker(),
        on_state=ducker.on_state,
    )


@pytest.mark.parametrize("ending", ["reply", "error", "reset", "interrupt"])
def test_every_way_a_turn_ends_restores_the_music(ending):
    client = FakeSpotify(volume=70)
    ducker = ducker_for(client)
    hold = asyncio.Event() if ending in ("reset", "interrupt") else None
    chat = Chat(error=LLMError("boom") if ending == "error" else None, hold=hold)
    orch = orchestrator(ducker, chat)

    async def scenario():
        await orch.handle("down")
        await orch.handle("up")
        if ending == "reset":
            await asyncio.sleep(0.05)
            await orch.handle("reset")
        elif ending == "interrupt":
            await asyncio.sleep(0.05)
            await orch.handle("down")  # cut off: listening again, still ducked
            hold.set()
            await orch.handle("up")
        await orch.wait_idle()

    asyncio.run(scenario())
    assert orch.state is State.IDLE
    settle(ducker)
    assert client.volumes == [14, 70]
    ducker.close()


def test_a_failing_duck_never_breaks_the_turn():
    client = FakeSpotify(fail=True)
    ducker = ducker_for(client)
    orch = orchestrator(ducker)

    async def scenario():
        await orch.handle("down")
        await orch.handle("up")
        await orch.wait_idle()

    asyncio.run(scenario())
    assert orch.state is State.IDLE
    assert len(orch.conversation) == 1  # the reply went through
    ducker.close()
