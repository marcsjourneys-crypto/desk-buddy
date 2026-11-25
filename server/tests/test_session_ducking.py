"""SessionDucker: other apps go down to a fifth while CARL is busy and come back exactly,
CARL's own process and Spotify are left alone, a user's change wins, and nothing ever
blocks or raises into the voice loop. Fake sessions only: the real mixer is never touched
(tests/integration has a read-only check)."""

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

import desk_buddy.__main__ as entry
from desk_buddy.config import Settings
from desk_buddy.loop import State
from desk_buddy.tools.ducking import SessionDucker
from desk_buddy.tools.winaudio import Session

OWN_PID = 100
NAMES = {100: "python.exe", 200: "chrome.exe", 300: "Spotify.exe", 400: "vlc.exe"}


class FakeApp:
    def __init__(self, sid, pid, level):
        self.sid, self.pid, self.level = sid, pid, level
        self.sets = []
        self.fail_set = None

    def session(self):
        def set_(level):
            if self.fail_set is not None:
                raise self.fail_set
            self.sets.append(level)
            self.level = level

        return Session(self.sid, self.pid, lambda: self.level, set_)


class FakeMixer:
    def __init__(self, *apps):
        self.apps = list(apps)
        self.fail = None
        self.gate = None  # a threading.Event that sessions() waits on
        self.threads = set()

    def sessions(self):
        self.threads.add(threading.current_thread().name)
        if self.gate is not None:
            self.gate.wait(5)
        if self.fail is not None:
            raise self.fail
        return [app.session() for app in self.apps]


def make(*apps, enabled=True, names=None):
    mixer = FakeMixer(*apps)
    names = NAMES if names is None else names
    ducker = SessionDucker(
        mixer,
        enabled=enabled,
        own_pid=OWN_PID,
        process_name=names.get,
        executor=ThreadPoolExecutor(max_workers=1, thread_name_prefix="test-duck"),
    )
    return ducker, mixer


def turn(ducker, *states):
    """Each state, waiting for its work (quick changes are coalesced: the worker acts on
    the latest state when it gets to it)."""
    for state in states:
        ducker.on_state(state)
        assert ducker.flush(2)


def test_other_apps_are_ducked_to_a_fifth_and_restored_exactly():
    chrome, vlc = FakeApp("c", 200, 0.8), FakeApp("v", 400, 0.5)
    ducker, mixer = make(chrome, vlc)
    turn(ducker, State.LISTENING)
    assert chrome.level == 0.8 * 0.2 and vlc.level == 0.5 * 0.2
    turn(ducker, State.THINKING, State.SPEAKING)  # still busy: no second duck
    assert len(chrome.sets) == 1
    turn(ducker, State.IDLE)
    assert chrome.level == 0.8 and vlc.level == 0.5
    assert mixer.threads == {"test-duck_0"}  # never on the caller's thread
    ducker.close()


def test_carl_spotify_unknown_and_silent_apps_are_left_alone():
    carl = FakeApp("me", OWN_PID, 1.0)
    spotify = FakeApp("s", 300, 0.9)
    unknown = FakeApp("u", 999, 0.7)  # its program can't be read
    system = FakeApp("sys", 0, 0.6)
    silent = FakeApp("z", 200, 0.0)
    chrome = FakeApp("c", 200, 0.5)
    ducker, _ = make(carl, spotify, unknown, system, silent, chrome)
    turn(ducker, State.LISTENING)
    assert [a.sets for a in (carl, spotify, unknown, system, silent)] == [[]] * 5
    assert chrome.level == 0.5 * 0.2
    turn(ducker, State.IDLE)
    assert chrome.level == 0.5
    ducker.close()


def test_a_level_the_user_changed_while_ducked_is_left_alone():
    chrome, vlc = FakeApp("c", 200, 0.8), FakeApp("v", 400, 0.5)
    ducker, _ = make(chrome, vlc)
    turn(ducker, State.LISTENING)
    chrome.level = 0.3  # the user moved it in the mixer
    turn(ducker, State.IDLE)
    assert chrome.level == 0.3
    assert vlc.level == 0.5
    ducker.close()


def test_sessions_that_ended_or_started_mid_turn_are_fine():
    chrome, vlc = FakeApp("c", 200, 0.8), FakeApp("v", 400, 0.5)
    ducker, mixer = make(chrome)
    turn(ducker, State.LISTENING)
    mixer.apps = [vlc]  # chrome closed, vlc started mid-turn
    turn(ducker, State.IDLE)
    assert vlc.sets == []  # not ducked, so nothing to restore
    turn(ducker, State.LISTENING)  # the next turn starts clean
    assert vlc.level == 0.5 * 0.2
    turn(ducker, State.IDLE)
    assert vlc.level == 0.5
    ducker.close()


def test_on_state_never_blocks_the_caller():
    chrome = FakeApp("c", 200, 0.8)
    ducker, mixer = make(chrome)
    mixer.gate = threading.Event()  # the mixer hangs
    done = threading.Event()

    def caller():
        ducker.on_state(State.LISTENING)
        ducker.on_state(State.IDLE)
        done.set()

    threading.Thread(target=caller).start()
    assert done.wait(1)
    mixer.gate.set()
    assert ducker.flush(2)
    ducker.close()


def test_failures_are_logged_once_per_kind_and_never_raise(caplog):
    caplog.set_level(logging.WARNING)
    chrome = FakeApp("c", 200, 0.8)
    ducker, mixer = make(chrome)
    mixer.fail = OSError("mixer gone")
    for _ in range(3):
        turn(ducker, State.LISTENING, State.IDLE)
    warnings = [r for r in caplog.records if "Ducking other apps failed" in r.getMessage()]
    assert len(warnings) == 1
    mixer.fail = None
    chrome.fail_set = OSError("access denied")
    turn(ducker, State.LISTENING, State.IDLE)
    turn(ducker, State.LISTENING, State.IDLE)
    warnings = [r for r in caplog.records if "Ducking other apps failed" in r.getMessage()]
    assert len(warnings) == 2  # one more kind: the duck itself
    assert chrome.level == 0.8
    ducker.close()


def test_a_failed_restore_is_tried_again_after_the_next_turn():
    chrome = FakeApp("c", 200, 0.8)
    ducker, mixer = make(chrome)
    turn(ducker, State.LISTENING)
    mixer.fail = OSError("busy")
    turn(ducker, State.IDLE)
    assert chrome.level == 0.8 * 0.2
    mixer.fail = None
    turn(ducker, State.LISTENING, State.IDLE)  # kept, not ducked twice
    assert chrome.level == 0.8
    assert len(chrome.sets) == 2
    ducker.close()


def test_a_name_lookup_that_raises_means_no_duck():
    chrome = FakeApp("c", 200, 0.8)

    def broken(pid):
        raise OSError("no access")

    mixer = FakeMixer(chrome)
    ducker = SessionDucker(
        mixer, own_pid=OWN_PID, process_name=broken, executor=ThreadPoolExecutor(1)
    )
    turn(ducker, State.LISTENING, State.IDLE)
    assert chrome.sets == []
    ducker.close()


def test_the_switch_off_restores_at_once_and_on_ducks_mid_turn():
    chrome = FakeApp("c", 200, 0.8)
    ducker, _ = make(chrome)
    turn(ducker, State.LISTENING)
    ducker.set_enabled(False, State.SPEAKING)
    assert ducker.flush(2) and chrome.level == 0.8
    turn(ducker, State.IDLE, State.LISTENING)  # off: nothing happens
    assert chrome.level == 0.8
    ducker.set_enabled(True, State.LISTENING)
    assert ducker.flush(2) and chrome.level == 0.8 * 0.2
    turn(ducker, State.IDLE)
    assert chrome.level == 0.8
    ducker.close()


def test_disabled_from_the_start_never_touches_the_mixer():
    chrome = FakeApp("c", 200, 0.8)
    ducker, mixer = make(chrome, enabled=False)
    turn(ducker, State.LISTENING, State.IDLE)
    assert mixer.threads == set()
    ducker.close()


def test_close_restores_mid_turn_and_later_states_do_nothing():
    chrome = FakeApp("c", 200, 0.8)
    ducker, _ = make(chrome)
    turn(ducker, State.LISTENING)
    ducker.close()
    assert chrome.level == 0.8
    ducker.on_state(State.LISTENING)  # after close: ignored, never raises
    ducker.close()  # twice is fine
    assert chrome.level == 0.8


# --- in the brain ---


class SpyDucker:
    def __init__(self):
        self.states, self.enabled, self.closed = [], [], False

    def on_state(self, state):
        self.states.append(state)

    def set_enabled(self, enabled, state="idle"):
        self.enabled.append(enabled)

    def close(self):
        self.closed = True


def test_the_brain_drives_it_by_music_duck_only(monkeypatch):
    monkeypatch.setattr(entry, "create_chat", lambda *a, **k: object())
    spy = SpyDucker()
    on = Settings(anthropic_api_key="k")  # no computer control: still ducks
    off = Settings(anthropic_api_key="k", music_duck=False)
    brain = entry.Brain(on, bus=entry.EventBus(), apps_ducker=spy)
    assert spy.enabled[-1] is True
    brain.on_state(State.LISTENING)
    assert spy.states == [State.LISTENING]
    brain.on_settings_now(on, off)  # off: restored at once
    assert spy.enabled[-1] is False
    brain.on_settings(on, off)
    assert spy.enabled[-1] is False
    brain.on_settings(off, on)
    assert spy.enabled[-1] is True
    brain.close()
    assert spy.closed


def test_startup_makes_one_and_logs_it(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    made = []

    class Fake:
        def __init__(self, *, enabled):
            made.append(enabled)

    monkeypatch.setattr(entry, "SessionDucker", Fake)
    assert entry._apps_ducker(Settings(anthropic_api_key="k")) is not None
    assert entry._apps_ducker(Settings(anthropic_api_key="k", music_duck=False)) is not None
    assert made == [True, False]
    assert "Ducking other apps: on" in caplog.messages
    assert "Ducking other apps: off (MUSIC_DUCK is off)" in caplog.messages

    def broken(**kwargs):
        raise OSError("no COM")

    monkeypatch.setattr(entry, "SessionDucker", broken)
    assert entry._apps_ducker(Settings(anthropic_api_key="k")) is None  # never fatal
