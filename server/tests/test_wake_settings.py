"""The wake word settings (S4): validation and precedence, the dashboard group, sending
wake_config to the robot, and the loop reading them at each hands-free start."""

import asyncio
import contextlib
import logging
from types import SimpleNamespace

import pytest

from desk_buddy.__main__ import hands_free_settings, send_wake_config
from desk_buddy.config import ConfigError, Settings, load_settings
from desk_buddy.dashboard import STATIC_DIR
from desk_buddy.fake_robot import FakeRobot
from desk_buddy.link import protocol
from desk_buddy.link.switch import DeviceSwitch
from desk_buddy.loop import HandsFree, State
from desk_buddy.server import create_app, start_server
from desk_buddy.settings_store import SettingsStore
from desk_buddy.vad import Endpointer

TOKEN = "t" * 20
BASE = {"ANTHROPIC_API_KEY": "k"}


# --- config ---


def test_wake_defaults():
    s = load_settings(env_path=None, environ=BASE)
    assert (s.wake_word, s.wake_threshold, s.follow_up_seconds, s.wake_end_silence) == (
        True,
        None,
        5.0,
        0.8,
    )


def test_wake_settings_are_read():
    s = load_settings(
        env_path=None,
        environ={
            **BASE,
            "WAKE_WORD": "off",
            "WAKE_THRESHOLD": "0.9",
            "FOLLOW_UP_SECONDS": "0",
            "WAKE_END_SILENCE": "1.25",
        },
    )
    assert (s.wake_word, s.wake_threshold, s.follow_up_seconds, s.wake_end_silence) == (
        False,
        0.9,
        0.0,
        1.25,
    )


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("WAKE_WORD", "maybe"),
        ("WAKE_THRESHOLD", "0.49"),
        ("WAKE_THRESHOLD", "1"),
        ("WAKE_THRESHOLD", "high"),
        ("WAKE_THRESHOLD", "nan"),
        ("FOLLOW_UP_SECONDS", "-1"),
        ("FOLLOW_UP_SECONDS", "15.5"),
        ("FOLLOW_UP_SECONDS", "inf"),
        ("WAKE_END_SILENCE", "0.3"),
        ("WAKE_END_SILENCE", "2.1"),
        ("WAKE_END_SILENCE", "soon"),
    ],
)
def test_bad_wake_values_are_clear_errors(key, value):
    with pytest.raises(ConfigError, match=key):
        load_settings(env_path=None, environ={**BASE, key: value})


def test_the_dashboard_wins_over_env_and_blank_means_the_default(tmp_path):
    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=k\nWAKE_THRESHOLD=0.9\nFOLLOW_UP_SECONDS=3\n")
    s = load_settings(env_path=env, environ={})
    assert (s.wake_threshold, s.follow_up_seconds) == (0.9, 3.0)
    s = load_settings(
        env_path=env, environ={}, dashboard={"WAKE_THRESHOLD": "0.95", "FOLLOW_UP_SECONDS": "8"}
    )
    assert (s.wake_threshold, s.follow_up_seconds) == (0.95, 8.0)
    s = load_settings(env_path=env, environ={}, dashboard={"WAKE_THRESHOLD": ""})
    assert s.wake_threshold is None  # blank on the dashboard: the default, the robot's own


# --- the dashboard's settings ---


@pytest.fixture
def store(tmp_path):
    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=k\nTTS_ENGINE=sapi\n", encoding="utf-8")
    return SettingsStore(tmp_path / "app" / "settings.json", env_path=env, environ={})


def test_the_wake_word_group_on_the_settings_tab(store):
    view = {e["key"]: e for e in store.view() if e["group"] == "Wake word"}
    assert list(view) == ["WAKE_WORD", "WAKE_THRESHOLD", "FOLLOW_UP_SECONDS", "WAKE_END_SILENCE"]
    assert view["WAKE_WORD"]["kind"] == "switch" and view["WAKE_WORD"]["value"] is True
    assert view["WAKE_THRESHOLD"]["value"] is None  # shown blank
    assert view["WAKE_WORD"]["applies"] == view["WAKE_THRESHOLD"]["applies"] == "at once"
    assert view["FOLLOW_UP_SECONDS"]["applies"] == "next wake"
    settings, errors = store.update({"WAKE_THRESHOLD": 0.93, "FOLLOW_UP_SECONDS": 0})
    assert errors == {}
    assert (settings.wake_threshold, settings.follow_up_seconds) == (0.93, 0.0)
    settings, errors = store.update({"WAKE_THRESHOLD": ""})
    assert errors == {} and settings.wake_threshold is None
    _, errors = store.update({"WAKE_END_SILENCE": 5})
    assert "WAKE_END_SILENCE" in errors


def test_the_live_tab_renders_the_wake_events_as_text():
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    for case in ('case "wake":', 'case "false_wake":', 'case "follow_up":'):
        assert case in js
    for words in ("Hey CARL", "False wake", "Listening for a follow-up", "Follow-up ended"):
        assert words in js
    assert "innerHTML" not in js


# --- wake_config to the robot ---


def test_wake_config_leaves_out_a_blank_threshold():
    assert protocol.wake_config(True) == {"type": "wake_config", "enabled": True}
    assert protocol.wake_config(False, 0.9)["threshold"] == 0.9


def settings(**changes):
    return Settings(anthropic_api_key="k", **changes)


class RecordingRobot:
    def __init__(self):
        self.sent = []

    def send_wake_config(self, enabled, threshold=None):
        self.sent.append((enabled, threshold))


@pytest.mark.parametrize(
    ("old", "new", "sent"),
    [
        ({}, {"wake_word": False}, [(False, None)]),
        ({}, {"wake_threshold": 0.9}, [(True, 0.9)]),
        ({"wake_threshold": 0.9}, {}, [(True, None)]),  # blank: the robot's own again
        ({}, {"follow_up_seconds": 2.0}, []),  # not the robot's business
    ],
)
def test_a_change_goes_to_the_robot_at_once(old, new, sent):
    robot = RecordingRobot()
    switch = SimpleNamespace(robot=robot)
    send_wake_config(switch, settings(**old), settings(**new))
    assert robot.sent == sent


def test_a_change_with_no_robot_does_nothing():
    send_wake_config(SimpleNamespace(robot=None), settings(), settings(wake_word=False))


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


@contextlib.asynccontextmanager
async def serving(wake_config):
    switch = DeviceSwitch(NullMic(), NullPlayer())
    app = create_app(
        token=TOKEN,
        switch=switch,
        events=asyncio.Queue(),
        current_state=lambda: "idle",
        wake_config=wake_config,
    )
    server = await start_server(app, "127.0.0.1", 0)
    try:
        yield f"ws://127.0.0.1:{server.port}/robot", switch
    finally:
        await asyncio.wait_for(server.stop(), 5)


def test_wake_config_after_welcome_and_on_a_dashboard_change():
    current = {"s": settings()}

    async def run():
        hook = lambda: (current["s"].wake_word, current["s"].wake_threshold)  # noqa: E731
        async with serving(hook) as (url, switch):
            robot = FakeRobot(url, TOKEN)
            await robot.connect()
            assert await robot.recv() == {"type": "welcome"}
            assert await robot.recv() == {"type": "wake_config", "enabled": True}
            assert await robot.recv() == {"type": "state", "name": "idle"}
            old, current["s"] = current["s"], settings(wake_threshold=0.95)
            send_wake_config(switch, old, current["s"])
            assert await robot.recv() == {
                "type": "wake_config",
                "enabled": True,
                "threshold": 0.95,
            }
            await robot.close()

    asyncio.run(run())


# --- the loop reads them at each hands-free start ---


def test_hands_free_settings_come_from_the_settings():
    hf = hands_free_settings(settings(wake_word=False, follow_up_seconds=2.5, wake_end_silence=1.2))
    assert (hf.enabled, hf.follow_up_seconds, hf.end_silence) == (False, 2.5, 1.2)
    assert hf.max_seconds == HandsFree().max_seconds


def make_orchestrator(hands_free, endpointer=Endpointer):
    from desk_buddy.brain.memory import Conversation
    from desk_buddy.loop import Orchestrator

    class HandsFreeMic(NullMic):
        def __init__(self):
            self.begun = 0

        def begin_hands_free(self, on_frame):
            self.begun += 1
            return True

        def end_hands_free(self, *, keep_recording=False):
            pass

    mic = HandsFreeMic()
    orch = Orchestrator(
        mic=mic,
        transcriber=SimpleNamespace(transcribe=lambda pcm: ""),
        chat=SimpleNamespace(),
        synthesizer=SimpleNamespace(),
        speaker=NullPlayer(),
        conversation=Conversation(),
        hands_free=hands_free,
        endpointer=endpointer,
    )
    return orch, mic


def test_wakes_are_ignored_when_the_wake_word_is_off(caplog):
    caplog.set_level(logging.DEBUG, logger="desk_buddy.loop")
    holder = {"hf": HandsFree(enabled=False)}
    orch, mic = make_orchestrator(lambda: holder["hf"])

    async def run():
        await orch.handle("wake 0.99")
        assert orch.state is State.IDLE and mic.begun == 0
        holder["hf"] = HandsFree()  # switched on: the next wake listens
        await orch.handle("wake 0.99")
        assert orch.state is State.LISTENING and mic.begun == 1
        await orch.handle("reset")

    asyncio.run(run())
    assert any("the wake word is off" in m for m in caplog.messages)


def test_the_end_silence_applies_at_the_next_wake():
    made = []

    def endpointer(**kwargs):
        made.append(kwargs)
        return Endpointer(**kwargs)

    holder = {"hf": HandsFree(end_silence=0.8)}
    orch, _ = make_orchestrator(lambda: holder["hf"], endpointer)

    async def run():
        await orch.handle("wake 0.99")
        await orch.handle("reset")
        holder["hf"] = HandsFree(end_silence=1.5)
        await orch.handle("wake 0.99")
        await orch.handle("reset")

    asyncio.run(run())
    assert [m["end_silence"] for m in made] == [0.8, 1.5]
    assert made[0]["no_speech_timeout"] == 4.0
    assert made[0]["ignore_first"] == 0.3
