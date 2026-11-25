"""open_app and open_website: enum schemas of spoken names, unknown names refused, launches
built by the server as argument lists (never a shell), everything through the guard."""

import logging
import os
import subprocess
import threading

import pytest

import desk_buddy.tools.launch as launch
from desk_buddy.apps_store import AppEntry, Bookmark
from desk_buddy.config import Settings
from desk_buddy.tools import build_tools
from desk_buddy.tools.actions import LIMIT_MESSAGE, OFF_MESSAGE, ActionGuard
from desk_buddy.tools.launch import LaunchTools
from desk_buddy.tools.registry import ToolError

NOTEPAD = AppEntry("Microsoft.WindowsNotepad_8wekyb3d8bbwe!App", "Notepad", ("notepad", "editor"))
EDGE = AppEntry("MSEdge", "Microsoft Edge", ("browser",))
WEATHER = Bookmark("weather", "https://example.com/weather")
NEWS = Bookmark("news", "https://news.example")


class RecordingBus:
    def __init__(self):
        self.events = []

    def publish(self, type_, **fields):
        self.events.append({"type": type_, **fields})


class Recorder:
    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail

    def __call__(self, value):
        self.calls.append(value)
        if self.fail is not None:
            raise self.fail


def make(enabled=True, *, apps=(NOTEPAD, EDGE), sites=(WEATHER, NEWS), bus=None, **kwargs):
    guard = ActionGuard(enabled, bus=bus)
    launcher, opener = Recorder(), Recorder()
    kwargs.setdefault("launcher", launcher)
    kwargs.setdefault("opener", opener)
    return LaunchTools(guard, apps, sites, **kwargs), guard, launcher, opener


def test_the_schemas_are_strict_enums_of_the_spoken_names():
    tools, *_ = make()
    schemas = {t.name: t.schema() for t in tools.tools()}
    assert list(schemas) == ["open_app", "open_website"]
    app = schemas["open_app"]["input_schema"]
    assert app["properties"]["name"]["enum"] == ["notepad", "editor", "browser"]
    assert app["required"] == ["name"] and app["additionalProperties"] is False
    site = schemas["open_website"]["input_schema"]
    assert site["properties"]["name"]["enum"] == ["weather", "news"]
    for schema in schemas.values():
        assert schema["strict"] is True
        text = str(schema)
        assert "MSEdge" not in text and "https://" not in text  # IDs and addresses stay here


def test_each_tool_only_exists_when_its_list_isnt_empty():
    assert [t.name for t in make(apps=())[0].tools()] == ["open_website"]
    assert [t.name for t in make(sites=())[0].tools()] == ["open_app"]
    assert make(apps=(), sites=())[0].tools() == []


def test_open_app_launches_the_chosen_app_by_its_id():
    tools, _, launcher, opener = make()
    assert tools.open_app("Editor") == "Opened Notepad."
    assert tools.open_app("browser") == "Opened Microsoft Edge."
    assert launcher.calls == [NOTEPAD.app_id, "MSEdge"]
    assert opener.calls == []


def test_open_website_opens_the_bookmarked_address():
    tools, _, launcher, opener = make()
    assert tools.open_website("weather") == "Opened weather."
    assert opener.calls == ["https://example.com/weather"]
    assert launcher.calls == []


@pytest.mark.parametrize(
    ("tool", "name"),
    [
        ("open_app", "cmd"),
        ("open_app", "notepad.exe"),
        ("open_app", "C:\\Windows\\System32\\cmd.exe"),
        ("open_app", "notepad & calc"),
        ("open_website", "https://evil.example"),
        ("open_website", "evil"),
    ],
)
def test_unknown_names_are_refused_through_the_guard_and_nothing_runs(tool, name, caplog):
    bus = RecordingBus()
    tools, guard, launcher, opener = make(bus=bus)
    with caplog.at_level(logging.INFO, logger="desk_buddy.tools.actions"):
        with pytest.raises(ToolError, match="isn't one of the"):
            getattr(tools, tool)(name)
    assert launcher.calls == [] and opener.calls == []
    assert guard._this_question == 1  # counted toward the limits like any attempt
    ((event),) = bus.events
    assert event["outcome"] == "refused" and event["tool"] == tool
    assert f"Action: {tool} " in caplog.text and "-> refused: That isn't one of" in caplog.text


def test_unknown_names_count_toward_the_limit():
    tools, guard, launcher, _ = make()
    for _ in range(3):
        with pytest.raises(ToolError, match="isn't one of the"):
            tools.open_app("cmd")
    with pytest.raises(ToolError, match="enough actions"):
        tools.open_app("notepad")
    assert launcher.calls == []


def test_the_real_launcher_is_one_argument_list_without_a_shell(monkeypatch):
    seen = []

    def fake_popen(args, **kwargs):
        seen.append((args, kwargs))

    monkeypatch.setattr(launch.subprocess, "Popen", fake_popen)
    launch.launch_app(NOTEPAD.app_id)
    ((args, kwargs),) = seen
    explorer = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "explorer.exe")
    assert args == [explorer, "shell:AppsFolder\\" + NOTEPAD.app_id]
    assert os.path.isabs(args[0])
    assert "shell" not in kwargs


def test_the_default_launcher_goes_through_the_real_one(monkeypatch):
    seen = []
    monkeypatch.setattr(launch.subprocess, "Popen", lambda args, **kw: seen.append((args, kw)))
    tools = LaunchTools(ActionGuard(True), [EDGE], [], opener=Recorder())
    assert tools.open_app("browser") == "Opened Microsoft Edge."
    assert seen == [([launch.EXPLORER, "shell:AppsFolder\\MSEdge"], {})]


def test_the_real_opener_uses_startfile(monkeypatch):
    opened = []
    monkeypatch.setattr(launch.os, "startfile", opened.append, raising=False)
    tools = LaunchTools(ActionGuard(True), [], [WEATHER], launcher=Recorder())
    assert tools.open_website("weather") == "Opened weather."
    assert opened == ["https://example.com/weather"]


def test_a_launch_that_fails_is_said_plainly():
    tools, *_ = make(launcher=Recorder(fail=OSError("no such app")))
    with pytest.raises(ToolError, match="I couldn't open Notepad."):
        tools.open_app("notepad")
    tools, *_ = make(opener=Recorder(fail=OSError("no browser")))
    with pytest.raises(ToolError, match="I couldn't open weather."):
        tools.open_website("weather")


def test_opening_a_page_never_waits_long():
    release = threading.Event()

    def slow(url):
        release.wait(5)

    tools, *_ = make(opener=slow, open_wait=0.05)
    try:
        assert tools.open_website("news") == "Opened news."
    finally:
        release.set()


def test_an_address_is_checked_again_right_before_it_opens():
    bad = Bookmark("evil", "file:///C:/Windows/System32/cmd.exe")  # never from the store
    tools, _, _, opener = make(sites=(bad,))
    with pytest.raises(ToolError, match="address isn't allowed"):
        tools.open_website("evil")
    assert opener.calls == []


def test_an_app_id_is_checked_again_right_before_it_starts():
    bad = AppEntry('x" & calc', "Bad", ("bad",))  # never from the store
    tools, _, launcher, _ = make(apps=(bad,))
    with pytest.raises(ToolError, match="I couldn't open Bad."):
        tools.open_app("bad")
    assert launcher.calls == []


def test_off_means_refused_and_nothing_runs():
    tools, _, launcher, opener = make(enabled=False)
    for call in (lambda: tools.open_app("notepad"), lambda: tools.open_website("weather")):
        with pytest.raises(ToolError, match=OFF_MESSAGE):
            call()
    assert launcher.calls == [] and opener.calls == []


def test_the_guard_limits_count_every_open():
    tools, guard, launcher, opener = make()
    tools.open_app("notepad")
    tools.open_website("weather")
    tools.open_app("browser")
    with pytest.raises(ToolError, match=LIMIT_MESSAGE):
        tools.open_website("news")
    assert len(launcher.calls) + len(opener.calls) == 3
    guard.new_question()
    tools.open_website("news")
    assert opener.calls[-1] == "https://news.example"


def test_every_open_is_logged_and_shown_on_the_dashboard(caplog):
    caplog.set_level(logging.INFO)
    events = []

    class Bus:
        def publish(self, type, **fields):
            events.append({"type": type, **fields})

    bus = Bus()
    tools, *_ = make(bus=bus, launcher=Recorder())
    tools.open_app("Notepad")
    bad, *_ = make(bus=bus, opener=Recorder(fail=OSError("x")))
    with pytest.raises(ToolError):
        bad.open_website("weather")
    off, *_ = make(enabled=False, bus=bus)
    with pytest.raises(ToolError):
        off.open_app("browser")
    actions = [(e["tool"], e["args"], e["outcome"]) for e in events if e["type"] == "action"]
    assert actions == [
        ("open_app", '{"name": "notepad"}', "ok"),
        ("open_website", '{"name": "weather"}', "failed"),
        ("open_app", '{"name": "browser"}', "refused"),
    ]
    assert 'Action: open_app {"name": "notepad"} -> Opened Notepad.' in caplog.messages


def test_the_registry_runs_them_by_name():
    tools, _, launcher, _ = make()
    registry = build_tools(Settings(anthropic_api_key="k"), launch=tools)
    assert "open_app" in registry and "open_website" in registry
    outcome = registry.run("open_app", {"name": "notepad"})
    assert (outcome.text, outcome.is_error) == ("Opened Notepad.", False)
    outcome = registry.run("open_app", {"name": "regedit"})
    assert outcome.is_error and launcher.calls == [NOTEPAD.app_id]


def test_nothing_here_ever_uses_a_shell():
    source = open(launch.__file__, encoding="utf-8").read()
    assert "shell=True" not in source
    assert "os.system" not in source
    assert subprocess.list2cmdline not in vars(launch).values()
