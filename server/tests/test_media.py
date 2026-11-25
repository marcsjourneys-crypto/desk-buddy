"""media_control: the right media key, down then up, through the guard. A fake sender
stands in for SendInput: no test ever presses a real key."""

import ctypes
import json
import logging

import pytest

import desk_buddy.__main__ as entry
from desk_buddy.brain.personality import system_prompt
from desk_buddy.config import Settings
from desk_buddy.tools import build_tools
from desk_buddy.tools import media as media_module
from desk_buddy.tools.actions import LIMIT_MESSAGE, OFF_MESSAGE, ActionGuard
from desk_buddy.tools.media import (
    BAD_ACTION,
    FAILED,
    INPUT,
    INPUT_KEYBOARD,
    KEYEVENTF_EXTENDEDKEY,
    KEYEVENTF_KEYUP,
    MediaKeys,
    make_inputs,
)
from desk_buddy.tools.registry import ToolError

EXT, UP = KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP


class RecordingBus:
    def __init__(self):
        self.events = []

    def publish(self, type_, **fields):
        self.events.append({"type": type_, **fields})


class FakeSender:
    def __init__(self, fail=None):
        self.sent = []
        self.fail = fail

    def __call__(self, events):
        self.sent.append(list(events))
        if self.fail is not None:
            raise self.fail


@pytest.fixture(autouse=True)
def no_real_keys(monkeypatch):
    def refuse(events):
        raise AssertionError("a test tried to press a real key")

    monkeypatch.setattr(media_module, "send_input", refuse)


def make(enabled=True, sender=None):
    bus = RecordingBus()
    guard = ActionGuard(enabled, bus=bus)
    sender = sender or FakeSender()
    return MediaKeys(guard, sender=sender), sender, bus, guard


@pytest.mark.parametrize(
    ("action", "vk"),
    [("play_pause", 0xB3), ("next", 0xB0), ("previous", 0xB1), ("stop", 0xB2)],
)
def test_each_action_presses_its_key_down_then_up(action, vk):
    keys, sender, bus, _ = make()
    assert keys.media_control(action) == "Done."
    assert sender.sent == [[(vk, EXT), (vk, EXT | UP)]]
    (event,) = bus.events
    assert event["tool"] == "media_control" and event["outcome"] == "ok"
    assert json.loads(event["args"]) == {"action": action}


def test_an_unknown_action_is_refused_and_presses_nothing():
    keys, sender, bus, _ = make()
    with pytest.raises(ToolError) as caught:
        keys.media_control("volume_up")
    assert str(caught.value) == BAD_ACTION
    assert sender.sent == []
    assert bus.events[-1]["outcome"] == "refused"


def test_a_failed_send_is_reported():
    keys, _, bus, _ = make(sender=FakeSender(fail=OSError("blocked")))
    with pytest.raises(ToolError) as caught:
        keys.media_control("next")
    assert str(caught.value) == FAILED
    assert bus.events[-1]["outcome"] == "failed"


def test_off_and_the_limits():
    keys, sender, *_ = make(enabled=False)
    with pytest.raises(ToolError, match=OFF_MESSAGE):
        keys.media_control("play_pause")
    assert sender.sent == []
    keys, sender, _, guard = make()
    for _ in range(3):
        keys.media_control("next")
    with pytest.raises(ToolError, match=LIMIT_MESSAGE):
        keys.media_control("next")
    assert len(sender.sent) == 3
    guard.new_question()
    keys.media_control("next")
    assert len(sender.sent) == 4


def test_the_schema_is_a_strict_enum():
    keys, *_ = make()
    (tool,) = keys.tools()
    schema = tool.schema()
    assert schema["name"] == "media_control" and schema["strict"] is True
    inp = schema["input_schema"]
    assert inp["properties"]["action"]["enum"] == ["play_pause", "next", "previous", "stop"]
    assert inp["required"] == ["action"] and inp["additionalProperties"] is False


def test_the_inputs_are_laid_out_as_send_input_expects():
    # Built only, never sent.
    assert ctypes.sizeof(INPUT) == (40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)
    inputs = make_inputs([(0xB3, EXT), (0xB3, EXT | UP)])
    assert len(inputs) == 2
    assert [i.type for i in inputs] == [INPUT_KEYBOARD] * 2
    assert [(i.u.ki.wVk, i.u.ki.dwFlags) for i in inputs] == [(0xB3, EXT), (0xB3, EXT | UP)]
    assert all(i.u.ki.wScan == 0 and i.u.ki.time == 0 for i in inputs)


def test_build_tools_and_the_prompt():
    keys, *_ = make()
    registry = build_tools(Settings(anthropic_api_key="k"), media=keys)
    assert "media_control" in registry
    with_music = system_prompt(2, media=True, music=True, computer_control=True)
    assert "For Spotify use music_control, never media_control." in with_music
    assert "If it's unclear which one" in with_music
    assert "play_pause is one toggle" in with_music
    without_music = system_prompt(2, media=True, computer_control=True)
    assert "media_control presses" in without_music
    assert "For Spotify use music_control" not in without_music
    off = system_prompt(2)
    assert "You can't pause, resume or skip videos" in off
    assert "media_control" not in off


@pytest.mark.parametrize(
    ("computer_control", "line"),
    [(True, "Media keys: on"), (False, "Media keys: off (COMPUTER_CONTROL is off)")],
)
def test_the_brain_offers_media_keys_with_computer_control(
    monkeypatch, caplog, computer_control, line
):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(entry, "create_chat", lambda *a, **k: object())
    brain = entry.Brain(
        Settings(anthropic_api_key="k", computer_control=computer_control), bus=entry.EventBus()
    )
    assert ("media_control" in brain.tools) is computer_control
    assert line in caplog.messages


def test_without_a_sender_the_real_one_is_used_at_each_press(monkeypatch):
    sent = []
    monkeypatch.setattr(media_module, "send_input", sent.append)  # instead of SendInput
    keys = MediaKeys(ActionGuard(True))
    keys.media_control("stop")
    assert sent == [[(0xB2, EXT), (0xB2, EXT | UP)]]
