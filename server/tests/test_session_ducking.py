"""SessionDucker: other apps go down to a fifth while CARL is busy and come back exactly,
CARL's own process (and Spotify while its API duck runs) are left alone, a user's change
wins, a crash is undone at the next start, and nothing ever blocks or raises into the
voice loop. Fake sessions only: the real mixer is never touched (tests/integration has a
read-only check)."""

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

import desk_buddy.__main__ as entry
from desk_buddy.config import Settings
from desk_buddy.loop import State
from desk_buddy.tools.ducking import DUCK_FACTOR, SessionDucker, ducked_apps_path
from desk_buddy.tools.winaudio import SESSION_ACTIVE, SESSION_INACTIVE, Session

OWN_PID = 100
NAMES = {
    100: "python.exe",
    200: "chrome.exe",
    201: "chrome.exe",
    202: "chrome.exe",
    300: "Spotify.exe",
    400: "vlc.exe",
}


class FakeApp:
    """One session in the fake mixer. `sid` is the stable session identifier."""

    def __init__(self, sid, pid, level, state=SESSION_ACTIVE):
        self.sid, self.pid, self.level, self.state = sid, pid, level, state
        self.sets = []
        self.fail_set = None
        self.stored = None  # how the mixer rounds a level it's given

    def session(self):
        def set_(level):
            if self.fail_set is not None:
                raise self.fail_set
            self.sets.append(level)
            self.level = self.stored(level) if self.stored else level

        return Session(self.sid, self.pid, self.state, lambda: self.level, set_)


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


@pytest.fixture(autouse=True)
def app_data(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))  # never the user's real file
    return tmp_path


def make(*apps, enabled=True, names=None, checks=(0.0,), mixer=None):
    mixer = mixer or FakeMixer(*apps)
    names = NAMES if names is None else names
    ducker = SessionDucker(
        mixer,
        enabled=enabled,
        own_pid=OWN_PID,
        process_name=names.get,
        executor=ThreadPoolExecutor(max_workers=1, thread_name_prefix="test-duck"),
        started_checks=checks,
    )
    assert ducker.flush(2)  # the startup check for a crash
    return ducker, mixer


def turn(ducker, *states):
    """Each state, waiting for its work (quick changes are coalesced: the worker acts on
    the latest state when it gets to it)."""
    for state in states:
        ducker.on_state(state)
        assert ducker.flush(2)


def saved_file():
    path = ducked_apps_path()
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def test_the_state_file_is_in_app_data(app_data):
    assert ducked_apps_path() == app_data / "desk-buddy" / "ducked_apps.json"


def test_other_apps_are_ducked_to_a_fifth_and_restored_exactly():
    chrome, vlc = FakeApp("c", 200, 0.8), FakeApp("v", 400, 0.5)
    ducker, mixer = make(chrome, vlc)
    turn(ducker, State.LISTENING)
    assert chrome.level == 0.8 * DUCK_FACTOR and vlc.level == 0.5 * DUCK_FACTOR
    turn(ducker, State.THINKING, State.SPEAKING)  # still busy: no second duck
    assert len(chrome.sets) == 1
    turn(ducker, State.IDLE)
    assert chrome.level == 0.8 and vlc.level == 0.5
    assert mixer.threads == {"test-duck_0"}  # never on the caller's thread
    ducker.close()


def test_carl_unknown_silent_and_inactive_sessions_are_left_alone():
    carl = FakeApp("me", OWN_PID, 1.0)
    unknown = FakeApp("u", 999, 0.7)  # its program can't be read
    system = FakeApp("sys", 0, 0.6)
    silent = FakeApp("z", 400, 0.0)
    paused = FakeApp("p", 400, 0.9, state=SESSION_INACTIVE)
    chrome = FakeApp("c", 200, 0.5)
    ducker, _ = make(carl, unknown, system, silent, paused, chrome)
    turn(ducker, State.LISTENING)
    assert [a.sets for a in (carl, unknown, system, silent, paused)] == [[]] * 5
    assert chrome.level == 0.5 * DUCK_FACTOR
    turn(ducker, State.IDLE)
    assert chrome.level == 0.5
    ducker.close()


def test_spotify_is_ducked_unless_its_api_duck_runs():
    spotify = FakeApp("s", 300, 0.9)
    ducker, _ = make(spotify)
    turn(ducker, State.LISTENING)  # the default: no API duck, so the mixer ducks it
    assert spotify.level == 0.9 * DUCK_FACTOR
    turn(ducker, State.IDLE)
    assert spotify.level == 0.9
    ducker.set_skip_spotify(True)
    turn(ducker, State.LISTENING, State.IDLE)
    assert len(spotify.sets) == 2  # untouched this time
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


def test_a_session_that_restarts_mid_turn_is_still_restored():
    chrome = FakeApp("c", 200, 0.8)
    ducker, mixer = make(chrome)
    turn(ducker, State.LISTENING)
    # The tab reloaded: a new session, same identifier and program, the ducked level kept.
    reloaded = FakeApp("c", 201, chrome.level)
    second = FakeApp("c", 202, chrome.level)  # and a second one of the same app
    mixer.apps = [reloaded, second]
    turn(ducker, State.IDLE)
    assert reloaded.level == 0.8 and second.level == 0.8
    ducker.close()


def test_the_mixers_rounding_is_what_counts_as_ours():
    chrome = FakeApp("c", 200, 0.8)
    chrome.stored = lambda level: round(level, 2) + 0.001  # stored a little off
    ducker, _ = make(chrome)
    turn(ducker, State.LISTENING)
    ((before, ducked),) = saved_file()["apps"][0]["levels"]
    assert before == 0.8 and ducked == pytest.approx(0.161)  # updated from the read-back
    turn(ducker, State.IDLE)
    assert chrome.sets[-1] == 0.8
    ducker.close()


def test_the_duck_is_recorded_before_the_level_changes():
    chrome = FakeApp("c", 200, 0.8)
    seen = []

    def stored(level):
        seen.append(saved_file())  # what's on disk when the mixer is set
        return level

    chrome.stored = stored
    ducker, _ = make(chrome)
    turn(ducker, State.LISTENING)
    (on_disk,) = seen
    assert on_disk["apps"][0]["levels"] == [[0.8, 0.8 * DUCK_FACTOR]]
    assert on_disk["apps"][0]["program"] == "chrome.exe" and on_disk["apps"][0]["session"] == "c"
    turn(ducker, State.IDLE)
    assert saved_file() is None  # deleted after a full restore
    ducker.close()


def test_new_sessions_are_ducked_when_a_tool_started_something():
    chrome, vlc = FakeApp("c", 200, 0.8), FakeApp("v", 400, 0.5)
    ducker, mixer = make(chrome)
    turn(ducker, State.LISTENING)
    mixer.apps.append(vlc)  # a tool opened a video
    ducker.started()
    assert ducker.flush(2)
    assert vlc.level == 0.5 * DUCK_FACTOR and len(chrome.sets) == 1  # not ducked twice
    turn(ducker, State.IDLE)
    assert chrome.level == 0.8 and vlc.level == 0.5
    ducker.started()  # idle: nothing to do
    assert ducker.flush(2) and vlc.level == 0.5
    ducker.close()


def test_started_looks_again_a_little_later():
    vlc = FakeApp("v", 400, 0.5)
    ducker, mixer = make(checks=(0.0, 0.3))
    turn(ducker, State.LISTENING)
    ducker.started()
    assert ducker.flush(2)
    mixer.apps.append(vlc)  # the browser starts playing a moment later
    for _ in range(100):
        if vlc.sets:
            break
        threading.Event().wait(0.02)
    assert ducker.flush(2) and vlc.level == 0.5 * DUCK_FACTOR
    turn(ducker, State.IDLE)
    ducker.close()


def test_on_state_never_blocks_the_caller():
    chrome = FakeApp("c", 200, 0.8)
    ducker, mixer = make(chrome)
    mixer.gate = threading.Event()  # the mixer hangs
    done = threading.Event()

    def caller():
        ducker.on_state(State.LISTENING)
        ducker.started()
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


def test_a_failed_listing_at_idle_is_tried_again_after_the_next_turn():
    chrome = FakeApp("c", 200, 0.8)
    ducker, mixer = make(chrome)
    turn(ducker, State.LISTENING)
    mixer.fail = OSError("busy")
    turn(ducker, State.IDLE)
    assert chrome.level == 0.8 * DUCK_FACTOR
    mixer.fail = None
    turn(ducker, State.LISTENING, State.IDLE)  # still ducked: not ducked twice
    assert chrome.level == 0.8
    assert len(chrome.sets) == 2
    ducker.close()


def test_a_failed_restore_is_retried_then_given_up(caplog):
    caplog.set_level(logging.WARNING)
    chrome = FakeApp("c", 200, 0.8)
    ducker, _ = make(chrome)
    turn(ducker, State.LISTENING)
    chrome.fail_set = OSError("denied")
    turn(ducker, State.IDLE)
    assert saved_file() is not None  # kept for the next idle
    turn(ducker, State.LISTENING, State.IDLE)
    chrome.fail_set = None
    turn(ducker, State.LISTENING, State.IDLE)  # the third try works
    assert chrome.level == 0.8 and saved_file() is None

    chrome.level = 0.6
    turn(ducker, State.LISTENING)
    chrome.fail_set = OSError("denied")
    turn(ducker, State.IDLE, State.LISTENING, State.IDLE, State.LISTENING)
    assert saved_file() is not None  # two failed tries: still kept
    turn(ducker, State.IDLE)
    assert saved_file() is None  # dropped after three failed tries
    assert any("giving up" in r.getMessage() for r in caplog.records)
    ducker.close()


def test_a_name_lookup_that_raises_means_no_duck():
    chrome = FakeApp("c", 200, 0.8)

    def broken(pid):
        raise OSError("no access")

    ducker = SessionDucker(
        FakeMixer(chrome), own_pid=OWN_PID, process_name=broken, executor=ThreadPoolExecutor(1)
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
    assert ducker.flush(2) and chrome.level == 0.8 * DUCK_FACTOR
    turn(ducker, State.IDLE)
    assert chrome.level == 0.8
    ducker.close()


def test_disabled_from_the_start_never_touches_the_mixer():
    chrome = FakeApp("c", 200, 0.8)
    ducker, mixer = make(chrome, enabled=False)
    turn(ducker, State.LISTENING, State.IDLE)
    assert chrome.sets == []
    ducker.close()


def test_close_restores_mid_turn_and_later_states_do_nothing():
    chrome = FakeApp("c", 200, 0.8)
    ducker, _ = make(chrome)
    turn(ducker, State.LISTENING)
    ducker.close()
    assert chrome.level == 0.8 and saved_file() is None
    ducker.on_state(State.LISTENING)  # after close: ignored, never raises
    ducker.started()
    ducker.close()  # twice is fine
    assert chrome.level == 0.8


# --- after a crash ---


def write_saved(*apps):
    path = ducked_apps_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"apps": list(apps)}), encoding="utf-8")


def test_a_crash_is_undone_at_the_next_start():
    write_saved(
        {"session": "c", "program": "chrome.exe", "levels": [[0.8, 0.16]]},
        {"session": "v", "program": "vlc.exe", "levels": [[0.5, 0.1]]},
    )
    chrome = FakeApp("c", 200, 0.16)
    vlc = FakeApp("v", 400, 0.4)  # changed by the user since
    ducker, _ = make(chrome, vlc)
    assert chrome.level == 0.8 and vlc.level == 0.4
    assert saved_file() is None
    ducker.close()


@pytest.mark.parametrize("text", ["not json", '{"apps": [{"session": 1}]}', '{"x": 1}'])
def test_a_bad_crash_file_is_dropped_never_fatal(text):
    path = ducked_apps_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    chrome = FakeApp("c", 200, 0.5)
    ducker, _ = make(chrome)
    assert chrome.sets == [] and not path.exists()
    turn(ducker, State.LISTENING, State.IDLE)
    assert chrome.level == 0.5
    ducker.close()


def test_an_unwritable_state_file_never_stops_ducking(tmp_path):
    chrome = FakeApp("c", 200, 0.8)
    blocker = tmp_path / "file"
    blocker.write_text("x")
    ducker = SessionDucker(
        FakeMixer(chrome),
        own_pid=OWN_PID,
        process_name=NAMES.get,
        executor=ThreadPoolExecutor(1),
        state_file=blocker / "sub" / "ducked_apps.json",  # can't be made
    )
    turn(ducker, State.LISTENING)
    assert chrome.level == 0.8 * DUCK_FACTOR
    turn(ducker, State.IDLE)
    assert chrome.level == 0.8
    ducker.close()


# --- in the brain ---


class SpyDucker:
    def __init__(self):
        self.states, self.enabled, self.skip, self.closed = [], [], [], False
        self.started_calls = 0

    def on_state(self, state):
        self.states.append(state)

    def set_enabled(self, enabled, state="idle"):
        self.enabled.append(enabled)

    def set_skip_spotify(self, skip):
        self.skip.append(skip)

    def started(self):
        self.started_calls += 1

    def close(self):
        self.closed = True


def test_the_brain_drives_it_by_music_duck_only(monkeypatch):
    monkeypatch.setattr(entry, "create_chat", lambda *a, **k: object())
    spy = SpyDucker()
    on = Settings(anthropic_api_key="k")  # no computer control: still ducks
    off = Settings(anthropic_api_key="k", music_duck=False)
    brain = entry.Brain(on, bus=entry.EventBus(), apps_ducker=spy)
    assert spy.enabled[-1] is True
    assert spy.skip[-1] is False  # no Spotify API duck: its session is ducked here
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


def test_spotify_is_skipped_only_while_the_music_ducker_runs(monkeypatch):
    from desk_buddy.tools.ducking import MusicDucker
    from desk_buddy.tools.spotify import MusicTools, SpotifyControl

    monkeypatch.setattr(entry, "create_chat", lambda *a, **k: object())
    spy = SpyDucker()
    control = SpotifyControl(object(), pc_name="pc")
    music_ducker = MusicDucker(control)
    guard = entry.ActionGuard(True)
    on = Settings(anthropic_api_key="k", computer_control=True)
    brain = entry.Brain(
        on,
        bus=entry.EventBus(),
        guard=guard,
        music=(MusicTools(control, guard, ducker=music_ducker), music_ducker),
        apps_ducker=spy,
    )
    assert spy.skip[-1] is True
    brain.on_settings_now(on, Settings(anthropic_api_key="k"))  # computer control off
    assert spy.skip[-1] is False
    brain.close()

    spy = SpyDucker()
    brain = entry.Brain(on, bus=entry.EventBus(), apps_ducker=spy)
    assert spy.skip[-1] is False  # no music tools yet
    brain._music_made(MusicTools(control, guard, ducker=music_ducker), music_ducker)
    assert spy.skip[-1] is True  # the music tools appeared
    brain.close()


def test_the_brains_youtube_and_launch_tools_tell_it(monkeypatch):
    monkeypatch.setattr(entry, "create_chat", lambda *a, **k: object())
    spy = SpyDucker()
    settings = Settings(anthropic_api_key="k", computer_control=True)
    brain = entry.Brain(settings, bus=entry.EventBus(), apps_ducker=spy)
    youtube = brain._youtube_tools(settings)
    youtube._opener = lambda url: None  # never a real browser
    youtube.youtube_search("bread")
    assert spy.started_calls == 1


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
