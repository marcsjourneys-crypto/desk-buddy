import logging
import threading

import pytest

from desk_buddy.tools.actions import ActionGuard
from desk_buddy.tools.registry import ToolError


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def make(enabled=True):
    clock = FakeClock()
    return ActionGuard(enabled, clock=clock), clock


def act(guard, name="music_control", result="Paused.", **args):
    return guard.run(name, args or {"action": "pause"}, lambda: result)


def test_an_allowed_action_runs_and_returns_its_result():
    guard, _ = make()
    assert act(guard) == "Paused."


def test_off_switch_refuses_everything_without_running_it():
    guard, _ = make(enabled=False)
    ran = []
    with pytest.raises(ToolError, match="off"):
        guard.run("music_control", {"action": "pause"}, lambda: ran.append(1) or "x")
    assert ran == []


def test_at_most_three_actions_per_question():
    guard, clock = make()
    guard.new_question()
    for _ in range(3):
        act(guard)
    with pytest.raises(ToolError, match="enough actions for one go"):
        act(guard)
    guard.new_question()  # the next question starts afresh
    assert act(guard) == "Paused."


def test_at_most_eight_actions_per_rolling_minute():
    guard, clock = make()
    for _ in range(8):
        guard.new_question()
        act(guard)
        clock.now += 5  # 8 actions over 35 s
    guard.new_question()
    with pytest.raises(ToolError, match="enough actions"):
        act(guard)
    clock.now += 20  # the first action is now 60 s old
    assert act(guard) == "Paused."


def test_a_refused_action_does_not_count():
    guard, clock = make()
    for _ in range(3):
        act(guard)
    for _ in range(5):
        with pytest.raises(ToolError):
            act(guard)
    guard.new_question()
    for _ in range(3):  # 6 so far in this minute: refusals didn't use up the budget
        act(guard)


def test_a_failed_action_still_counts():
    guard, _ = make()

    def boom():
        raise ToolError("Spotify didn't open.")

    for _ in range(3):
        with pytest.raises(ToolError, match="didn't open"):
            guard.run("play_music", {"query": "x", "kind": "track"}, boom)
    with pytest.raises(ToolError, match="enough actions"):
        act(guard)


def test_every_action_is_logged_on_one_line(caplog):
    caplog.set_level(logging.INFO, logger="desk_buddy.tools.actions")
    guard, _ = make()
    guard.run("play_music", {"query": "Queen", "kind": "artist"}, lambda: "Playing\nQueen.")
    for _ in range(2):
        act(guard)
    with pytest.raises(ToolError):
        act(guard)

    def boom():
        raise ToolError("I can't reach Spotify right now.")

    guard.new_question()
    with pytest.raises(ToolError):
        guard.run("now_playing", {}, boom)
    lines = [r.getMessage() for r in caplog.records]
    assert lines[0] == 'Action: play_music {"kind": "artist", "query": "Queen"} -> Playing Queen.'
    assert lines[3] == 'Action: music_control {"action": "pause"} -> refused: rate limit'
    assert lines[4] == "Action: now_playing {} -> failed: I can't reach Spotify right now."
    assert all("\n" not in line for line in lines)


def test_long_args_are_cut_short_in_the_log(caplog):
    caplog.set_level(logging.INFO, logger="desk_buddy.tools.actions")
    guard, _ = make()
    guard.run("play_music", {"query": "x" * 10_000, "kind": "track"}, lambda: "ok")
    (line,) = caplog.messages
    assert len(line) < 400
    assert line.endswith("-> ok")


def test_args_that_arent_json_still_log():
    guard, _ = make()

    class Odd:
        def __repr__(self):
            return "Odd()"

    assert guard.run("x", {"thing": Odd(), "raw": b"\x00"}, lambda: "done") == "done"


def test_off_switch_refusals_are_logged(caplog):
    caplog.set_level(logging.INFO, logger="desk_buddy.tools.actions")
    guard, _ = make(enabled=False)
    with pytest.raises(ToolError):
        act(guard)
    assert caplog.messages == ['Action: music_control {"action": "pause"} -> refused: off']


def test_the_limits_hold_across_threads():
    guard, _ = make()
    done = []

    def try_one():
        try:
            done.append(act(guard))
        except ToolError:
            pass

    threads = [threading.Thread(target=try_one) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert len(done) == 3
