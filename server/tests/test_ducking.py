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


def test_close_restores_even_without_an_idle_state():
    # Shutdown mid-turn, or after a farewell that timed out: CARL never went idle.
    client = FakeSpotify(volume=40, delay=0.1)
    ducker = ducker_for(client)
    ducker.on_state(State.SPEAKING)
    ducker.close()
    assert client.volumes == [8, 40]
    ducker.on_state(State.LISTENING)  # ignored once closed
    ducker.close()  # twice is fine
    assert client.volumes == [8, 40]


def tools_for(client, ducker_control=None):
    control = SpotifyControl(client, pc_name="pc")
    ducker = MusicDucker(ducker_control or control)
    return ToolRegistry(MusicTools(control, ActionGuard(True), ducker=ducker).tools()), ducker


def test_a_failed_user_volume_change_keeps_the_restore():
    client = FakeSpotify(volume=80)
    tools, ducker = tools_for(client)
    ducker.on_state(State.LISTENING)
    settle(ducker)
    good = client.volume

    def broken(volume_percent, device_id=None):
        raise SpotifyException(502, -1, "Bad gateway")

    client.volume = broken
    assert tools.run("music_volume", {"level": 30, "change": None}).is_error
    client.volume = good
    ducker.on_state(State.IDLE)
    settle(ducker)
    assert client.volumes == [16, 80]  # the music still comes back
    ducker.close()


def test_a_duck_that_lands_but_times_out_is_still_restored():
    import requests

    client = FakeSpotify(volume=60)
    good = client.volume

    def lands_then_times_out(volume_percent, device_id=None):
        good(volume_percent, device_id)
        raise requests.exceptions.ReadTimeout("slow")

    client.volume = lands_then_times_out
    ducker = ducker_for(client)
    ducker.on_state(State.LISTENING)
    settle(ducker)
    client.volume = good
    ducker.on_state(State.IDLE)
    settle(ducker)
    assert client.volumes == [12, 60]
    ducker.close()


def test_a_duck_spotify_refused_is_not_restored():
    client = FakeSpotify(volume=60)
    good = client.volume

    def refused(volume_percent, device_id=None):
        raise SpotifyException(403, -1, "VOLUME_CONTROL_DISALLOW", reason="UNKNOWN")

    client.volume = refused
    ducker = ducker_for(client)
    ducker.on_state(State.LISTENING)
    settle(ducker)
    client.volume = good
    ducker.on_state(State.IDLE)
    settle(ducker)
    assert client.volumes == []  # nothing changed, so nothing to put back
    ducker.close()


def test_the_users_level_wins_over_a_duck_in_flight():
    client = FakeSpotify(volume=80, delay=0.3)  # the duck is slow
    tools, ducker = tools_for(client)
    ducker.on_state(State.LISTENING)
    time.sleep(0.1)  # the duck is now reading the playback state
    assert not tools.run("music_volume", {"level": 50, "change": None}).is_error
    ducker.on_state(State.IDLE)
    settle(ducker)
    assert client.volumes == [16, 50]  # the user's 50 stays: no restore to 80
    assert client.volume_percent == 50
    ducker.close()


def test_music_started_mid_turn_is_ducked_under_the_reply():
    client = FakeSpotify(volume=70, is_playing=False)
    control = SpotifyControl(client, pc_name="pc")
    ducker = MusicDucker(control)
    ducker.on_state(State.LISTENING)  # nothing playing yet: no duck
    settle(ducker)
    assert client.volumes == []
    ducker.music_started()  # a tool just started the music (Spotify may lag on is_playing)
    settle(ducker)
    assert client.volumes == [14]
    ducker.on_state(State.IDLE)
    settle(ducker)
    assert client.volumes == [14, 70]
    ducker.close()


def test_music_started_when_idle_does_nothing():
    client = FakeSpotify(volume=70)
    ducker = ducker_for(client)
    ducker.music_started()
    settle(ducker)
    assert client.volumes == []
    ducker.close()


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


@pytest.mark.parametrize("ending", ["reply", "error", "reset", "interrupt", "farewell", "shutdown"])
def test_every_way_a_turn_ends_restores_the_music(ending):
    client = FakeSpotify(volume=70)
    ducker = ducker_for(client)
    hold = asyncio.Event() if ending not in ("reply", "error") else None
    chat = Chat(error=LLMError("boom") if ending == "error" else None, hold=hold)
    orch = orchestrator(ducker, chat)

    async def scenario():
        await orch.handle("down")
        await orch.handle("up")
        if ending in ("reply", "error"):
            await orch.wait_idle()
            return
        await asyncio.sleep(0.05)  # mid-turn: thinking, music ducked
        if ending == "reset":
            await orch.handle("reset")
        elif ending == "interrupt":
            await orch.handle("down")  # cut off: listening again, still ducked
            hold.set()
            await orch.handle("up")
            await orch.wait_idle()
        elif ending == "farewell":
            await orch.farewell(timeout=2)
        else:  # Ctrl+C before the farewell: the turn is cancelled, CARL never goes idle
            await orch.shutdown()

    asyncio.run(scenario())
    if ending == "shutdown":
        assert orch.state is not State.IDLE
        ducker.close()  # what the exit stack does: it restores anyway
    else:
        assert orch.state is State.IDLE
        settle(ducker)
        ducker.close()
    assert client.volumes[0] == 14
    assert client.volumes[-1] == 70


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
