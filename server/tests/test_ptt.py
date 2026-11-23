import threading

import pytest

from desk_buddy.ptt import EdgeDetector, KeyWatcher, parse_key


def test_parse_key_names_and_hex():
    assert parse_key("right_ctrl") == 0xA3
    assert parse_key("  F8 ") == 0x77
    assert parse_key("0xA5") == 0xA5


@pytest.mark.parametrize("bad", ["banana", "0x", "0xZZ", "0x100", ""])
def test_parse_key_rejects_unknown(bad):
    with pytest.raises(ValueError, match="unknown key"):
        parse_key(bad)


def test_edge_detector_reports_only_changes():
    det = EdgeDetector()
    readings = [False, True, True, True, False, False, True, False]
    events = [det.update(r) for r in readings]
    assert events == [None, "down", None, None, "up", None, "down", "up"]


def test_key_watcher_emits_down_then_up():
    readings = iter([False, True, True, False])
    events: list[str] = []
    done = threading.Event()

    def on_event(ev: str) -> None:
        events.append(ev)
        if ev == "up":
            done.set()

    watcher = KeyWatcher(0xA3, on_event, read_key=lambda vk: next(readings, False), interval=0.001)
    watcher.start()
    assert done.wait(timeout=2)
    watcher.stop()
    assert events == ["down", "up"]
