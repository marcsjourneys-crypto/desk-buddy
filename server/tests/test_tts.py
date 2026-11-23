"""Unit tests for SapiSynthesizer's lifecycle, with comtypes faked out."""

import threading

import comtypes
import comtypes.client
import pytest

from desk_buddy import tts
from desk_buddy.tts import SapiSynthesizer


class FakeVoice:
    def __init__(self) -> None:
        self.AudioOutputStream = object()
        self.spoken: list[str] = []
        self.streams: list[object] = []

    def Speak(self, text, flags):  # noqa: N802 - COM name
        self.spoken.append(text)
        self.streams.append(self.AudioOutputStream)


class FakeFormat:
    Type = 0


class FakeStream:
    def __init__(self, data) -> None:
        self.Format = FakeFormat()
        self._data = data

    def GetData(self):  # noqa: N802 - COM name
        return self._data


@pytest.fixture
def com(monkeypatch):
    """Fake COM: records which thread (un)initializes and what gets created."""
    state = {"voice": FakeVoice(), "data": (1, 2, 3, 4), "uninit": [], "fail_create": False}

    def create(progid):
        if progid == "SAPI.SpVoice":
            if state["fail_create"]:
                raise OSError("no SAPI")
            return state["voice"]
        if progid == "SAPI.SpMemoryStream":
            return FakeStream(state["data"])
        return FakeFormat()

    monkeypatch.setattr(comtypes.client, "CreateObject", create)
    monkeypatch.setattr(comtypes, "CoInitialize", lambda: None)
    monkeypatch.setattr(
        comtypes, "CoUninitialize", lambda: state["uninit"].append(threading.current_thread().name)
    )
    return state


def test_create_failure_shuts_down_worker_and_reraises(com, monkeypatch):
    shutdowns = []

    class SpyExecutor(tts.ThreadPoolExecutor):
        def shutdown(self, *args, **kwargs):
            shutdowns.append(self)
            super().shutdown(*args, **kwargs)

    monkeypatch.setattr(tts, "ThreadPoolExecutor", SpyExecutor)
    com["fail_create"] = True
    with pytest.raises(OSError, match="no SAPI"):
        SapiSynthesizer()
    assert len(shutdowns) == 1


def test_empty_text_skips_com(com):
    s = SapiSynthesizer()
    try:
        assert s.synthesize("") == b""
        assert s.synthesize("  \n") == b""
        assert com["voice"].spoken == []
    finally:
        s.close()


def test_synthesize_returns_bytes_and_releases_stream(com):
    s = SapiSynthesizer()
    try:
        assert s.synthesize("hi") == bytes([1, 2, 3, 4])
        assert isinstance(com["voice"].streams[0], FakeStream)
        assert com["voice"].AudioOutputStream is None
    finally:
        s.close()


@pytest.mark.parametrize("data", [None, (), b""])
def test_no_data_gives_empty_bytes(com, data):
    com["data"] = data
    s = SapiSynthesizer()
    try:
        assert s.synthesize("hi") == b""
    finally:
        s.close()


def test_close_uninitializes_on_com_thread_once(com):
    s = SapiSynthesizer()
    s.close()
    s.close()
    assert len(com["uninit"]) == 1
    assert com["uninit"][0].startswith("sapi")
    assert s._voice is None
