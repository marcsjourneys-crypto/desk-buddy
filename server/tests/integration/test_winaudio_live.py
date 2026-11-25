"""The real Windows volume mixer, read only: list the sessions and read their levels and
programs. Nothing is ever set. Run with: uv run pytest -m integration"""

import pytest

from desk_buddy.tools.winaudio import SessionBackend, com_worker, process_name

pytestmark = pytest.mark.integration


def test_the_real_sessions_can_be_listed_and_read():
    worker = com_worker("winaudio-test")

    def read():
        sessions = SessionBackend().sessions()
        return [(s.id, s.pid, s.get()) for s in sessions]  # get only, never set

    try:
        found = worker.submit(read).result(timeout=10)
    finally:
        worker.shutdown(wait=True)
    for session_id, pid, level in found:
        assert isinstance(session_id, str) and session_id
        assert pid >= 0
        assert 0.0 <= level <= 1.0
    print([(pid, process_name(pid), round(level, 2)) for _, pid, level in found])


def test_this_process_has_a_name():
    import os

    assert process_name(os.getpid()) in ("python.exe", "pythonw.exe")
    assert process_name(0) is None
