import asyncio
import json
import logging
import threading
import time

import pytest

import desk_buddy.__main__ as entry
from desk_buddy.audio import AudioDeviceError
from desk_buddy.config import Settings
from desk_buddy.link.switch import DeviceSwitch, SwitchedMic, SwitchedPlayer
from desk_buddy.server import ServerStartError
from desk_buddy.tts import VoiceError

TOKEN = "s3cret-token-1234567"


@pytest.fixture
def calls():
    return []


@pytest.fixture
def fakes(monkeypatch, calls, tmp_path):
    """Patch every device and model in __main__ with fakes that record their closing."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))  # never the user's real app data

    class FakeTranscriber:
        def __init__(self, *args, **kwargs):
            calls.append("transcriber created")

    class FakeSynthesizer:
        def __init__(self, *args, **kwargs):
            calls.append("synthesizer created")
            if args or kwargs:
                calls.append(("synthesizer args", args, kwargs))

        def close(self):
            calls.append("synthesizer closed")

        def set_speed(self, speed):
            calls.append(("synthesizer speed", speed))

    class FakeMicrophone:
        def __init__(self, *args, **kwargs):
            calls.append("mic created")

        def close(self):
            calls.append("mic closed")

    class FakeSpeaker:
        def __init__(self, *args, **kwargs):
            calls.append("speaker created")

        def close(self):
            calls.append("speaker closed")

    class FakeOrchestrator:
        def __init__(self, **kwargs):
            calls.append(("orchestrator kwargs", kwargs))
            self.state = entry.State.IDLE

        async def run(self, events):
            calls.append(("events at run", list(events._queue), events))
            raise KeyboardInterrupt  # what Ctrl+C looks like inside the loop

        async def farewell(self, timeout):
            calls.append(("farewell timeout", timeout))
            calls.append("farewell")

        async def shutdown(self):
            calls.append("orchestrator shutdown")

    class FakeWatcher:
        def __init__(self, vk, on_event):
            pass

        def start(self):
            calls.append("watcher started")

        def stop(self):
            calls.append("watcher stopped")

    monkeypatch.setattr(entry, "load_settings", lambda: Settings(anthropic_api_key="k"))
    monkeypatch.setattr(entry, "check_audio_devices", lambda: None)
    monkeypatch.setattr(entry, "WhisperTranscriber", FakeTranscriber)
    monkeypatch.setattr(entry, "SapiSynthesizer", FakeSynthesizer)

    def fake_load_piper(voice, voices_dir, *, speed):
        calls.append(("load_piper", voice, voices_dir, speed))
        return FakeSynthesizer()

    monkeypatch.setattr(entry, "load_piper", fake_load_piper)
    monkeypatch.setattr(entry, "Microphone", FakeMicrophone)
    monkeypatch.setattr(entry, "Speaker", FakeSpeaker)
    monkeypatch.setattr(entry, "create_chat", lambda settings, tools=None, **_: object())
    monkeypatch.setattr(entry, "Orchestrator", FakeOrchestrator)
    monkeypatch.setattr(entry, "KeyWatcher", FakeWatcher)

    class FakeServer:
        port = 8765

        async def stop(self):
            calls.append("server stopped")

    def fake_create_app(**kwargs):
        calls.append(("create_app", kwargs))
        return "app"

    async def fake_start_server(app, host, port):
        calls.append(("start_server", app, host, port))
        return FakeServer()

    monkeypatch.setattr(entry, "create_app", fake_create_app)
    monkeypatch.setattr(entry, "start_server", fake_start_server)

    class FakeDashboard:
        async def stop(self):
            calls.append("dashboard stopped")

    async def fake_start_dashboard(app, port):
        calls.append(("start_dashboard", app, port))
        return FakeDashboard()

    monkeypatch.setattr(entry, "start_dashboard", fake_start_dashboard)

    class FakeSessionDucker:  # never the real volume mixer
        def __init__(self, *, enabled):
            calls.append(("apps ducker", enabled))

        def on_state(self, state):
            pass

        def set_enabled(self, enabled, state="idle"):
            calls.append(("apps ducker enabled", enabled))

        def set_skip_spotify(self, skip):
            calls.append(("apps ducker skips spotify", skip))

        def started(self):
            pass

        def close(self):
            calls.append(("apps ducker", "closed"))

    monkeypatch.setattr(entry, "SessionDucker", FakeSessionDucker)


def steps(calls):
    """The plain string entries (created/closed/started/stopped)."""
    return [c for c in calls if isinstance(c, str)]


def _raise(exc):
    def factory(*args, **kwargs):
        raise exc

    return factory


def test_mic_failure_prints_reason_and_closes_what_was_built(monkeypatch, capsys, fakes, calls):
    monkeypatch.setattr(entry, "Microphone", _raise(AudioDeviceError("no mic")))

    assert entry.main() == 2

    err = capsys.readouterr().err
    assert "Can't start" in err
    assert "no mic" in err
    assert steps(calls) == ["transcriber created", "synthesizer created", "synthesizer closed"]


def test_whisper_failure_is_a_clear_startup_error(monkeypatch, capsys, fakes, calls):
    monkeypatch.setattr(entry, "WhisperTranscriber", _raise(ValueError("bad model")))

    assert entry.main() == 2

    err = capsys.readouterr().err
    assert "Can't start: Can't load Whisper model 'base.en' on cpu (int8): bad model" in err
    assert "Traceback" not in err
    assert calls == []  # nothing was built before it, so nothing to close


PIPER = Settings(
    anthropic_api_key="k",
    tts_engine="piper",
    tts_voice="en_GB-northern_english_male-medium",
    tts_speed=1.25,
    voices_dir="V",
)


def test_piper_engine_finds_and_loads_the_voice(monkeypatch, fakes, calls, caplog):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(entry, "load_settings", lambda: PIPER)

    assert entry.main() == 0

    assert ("load_piper", "en_GB-northern_english_male-medium", "V", 1.25) in calls
    assert "Voice: piper en_GB-northern_english_male-medium" in caplog.messages
    assert "synthesizer closed" in steps(calls)


def test_sapi_engine_logs_its_voice(monkeypatch, fakes, calls, caplog):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(
        entry, "load_settings", lambda: Settings(anthropic_api_key="k", tts_engine="sapi")
    )
    assert entry.main() == 0
    assert "Voice: sapi" in caplog.messages
    assert not [c for c in calls if isinstance(c, tuple) and c[0] == "load_piper"]


@pytest.mark.parametrize(
    "message",
    [
        "Couldn't get voice 'x': HTTP Error 404",
        "Can't load Piper voice 'en_GB-northern_english_male-medium': bad model",
    ],
)
def test_a_piper_voice_failure_is_a_clear_startup_error(monkeypatch, capsys, fakes, calls, message):
    monkeypatch.setattr(entry, "load_settings", lambda: PIPER)
    monkeypatch.setattr(entry, "load_piper", _raise(VoiceError(message)))

    assert entry.main() == 2

    err = capsys.readouterr().err
    assert f"Can't start: {message}; or set TTS_ENGINE=sapi to use the Windows voice" in err
    assert "Traceback" not in err
    assert calls == ["transcriber created"]


def test_sapi_failure_is_a_clear_startup_error(monkeypatch, capsys, fakes, calls):
    monkeypatch.setattr(
        entry, "load_settings", lambda: Settings(anthropic_api_key="k", tts_engine="sapi")
    )
    monkeypatch.setattr(entry, "SapiSynthesizer", _raise(OSError("no voice")))

    assert entry.main() == 2

    err = capsys.readouterr().err
    assert "Can't start: Can't start Windows SAPI voice: no voice" in err
    assert "Traceback" not in err
    assert calls == ["transcriber created"]  # the transcriber has nothing to close


def test_ctrl_c_shuts_down_in_reverse_creation_order(fakes, calls):
    assert entry.main() == 0

    assert steps(calls) == [
        "transcriber created",
        "synthesizer created",
        "mic created",
        "speaker created",
        "watcher started",
        "farewell",
        "watcher stopped",
        "dashboard stopped",
        "orchestrator shutdown",
        "speaker closed",
        "mic closed",
        "synthesizer closed",
    ]


def test_ready_queues_the_startup_greeting(fakes, calls):
    entry.main()
    ((_, queued, _),) = [c for c in calls if isinstance(c, tuple) and c[0] == "events at run"]
    assert queued == ["greet"]


def test_farewell_is_bounded_to_five_seconds_and_replaces_bye(fakes, calls, caplog):
    caplog.set_level(logging.INFO)
    entry.main()
    assert ("farewell timeout", 5.0) in calls
    assert entry.FAREWELL_HARD_CAP == 6.0
    assert "Bye" not in caplog.messages


@pytest.mark.parametrize("failure", ["hang", "raise"])
def test_a_failed_farewell_does_not_block_shutdown(monkeypatch, fakes, calls, failure):
    monkeypatch.setattr(entry, "FAREWELL_TIMEOUT", 0.05)
    monkeypatch.setattr(entry, "FAREWELL_HARD_CAP", 0.1)
    orchestrator_class = entry.Orchestrator

    class BadFarewell(orchestrator_class):
        async def farewell(self, timeout):
            if failure == "hang":
                await asyncio.sleep(60)  # ignores its own timeout
            raise RuntimeError("no voice")

    monkeypatch.setattr(entry, "Orchestrator", BadFarewell)
    started = time.monotonic()
    assert entry.main() == 0
    assert time.monotonic() - started < 5
    assert steps(calls)[-4:] == [
        "orchestrator shutdown",
        "speaker closed",
        "mic closed",
        "synthesizer closed",
    ]


def test_robot_link_off_without_token(fakes, calls, caplog):
    caplog.set_level(logging.INFO)
    assert entry.main() == 0
    assert not [c for c in calls if isinstance(c, tuple) and c[0] == "start_server"]
    assert "Robot link: off (no ROBOT_TOKEN)" in caplog.text


def test_robot_link_on_with_token(monkeypatch, fakes, calls, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(
        entry, "load_settings", lambda: Settings(anthropic_api_key="k", robot_token=TOKEN)
    )

    assert entry.main() == 0

    assert ("start_server", "app", "127.0.0.1", 8765) in calls
    (app_kwargs,) = [c[1] for c in calls if isinstance(c, tuple) and c[0] == "create_app"]
    assert app_kwargs["token"] == TOKEN
    assert isinstance(app_kwargs["switch"], DeviceSwitch)
    # The server starts after the orchestrator is built and before the watcher.
    names = [c if isinstance(c, str) else c[0] for c in calls]
    assert names.index("orchestrator kwargs") < names.index("start_server")
    assert names.index("start_server") < names.index("watcher started")
    shutdown = steps(calls)
    assert shutdown[shutdown.index("farewell") :][:5] == [
        "farewell",  # before the server and the devices close
        "watcher stopped",
        "dashboard stopped",
        "server stopped",
        "orchestrator shutdown",
    ]
    # A robot connecting asks for a greeting, after its reset.
    ((_, _, events),) = [c for c in calls if isinstance(c, tuple) and c[0] == "events at run"]
    app_kwargs["on_connect"]()
    assert list(events._queue)[-1] == "greet"
    assert "Robot link: on (ws://127.0.0.1:8765/robot)" in caplog.text
    assert TOKEN not in caplog.text


@pytest.mark.parametrize("host", ["0.0.0.0", "::", ""])
def test_a_wildcard_host_logs_the_lan_address(monkeypatch, fakes, calls, caplog, host):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(
        entry,
        "load_settings",
        lambda: Settings(anthropic_api_key="k", robot_token=TOKEN, server_host=host),
    )
    monkeypatch.setattr(entry, "lan_ip", lambda: "192.168.1.20")

    assert entry.main() == 0

    assert (
        "Robot link: on (ws://192.168.1.20:8765/robot — set the robot with: "
        "server 192.168.1.20:8765)" in caplog.text
    )
    assert TOKEN not in caplog.text


def test_a_wildcard_host_without_a_lan_address_logs_the_host(monkeypatch, fakes, calls, caplog):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(
        entry,
        "load_settings",
        lambda: Settings(anthropic_api_key="k", robot_token=TOKEN, server_host="0.0.0.0"),
    )
    monkeypatch.setattr(entry, "lan_ip", lambda: None)

    assert entry.main() == 0

    assert "Robot link: on (ws://0.0.0.0:8765/robot)" in caplog.text


def test_lan_ip_reads_the_outgoing_interface(monkeypatch):
    class FakeSocket:
        def __init__(self, family, kind):
            assert kind == entry.socket.SOCK_DGRAM

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def connect(self, address):
            assert address == ("192.0.2.1", 80)

        def getsockname(self):
            return ("192.168.1.20", 50000)

    monkeypatch.setattr(entry.socket, "socket", FakeSocket)
    assert entry.lan_ip() == "192.168.1.20"


def test_lan_ip_is_none_without_a_network(monkeypatch):
    class NoNetwork:
        def __init__(self, family, kind):
            raise OSError("network unreachable")

    monkeypatch.setattr(entry.socket, "socket", NoNetwork)
    assert entry.lan_ip() is None


def test_the_orchestrator_uses_the_device_switch(fakes, calls):
    entry.main()
    (kwargs,) = [c[1] for c in calls if isinstance(c, tuple) and c[0] == "orchestrator kwargs"]
    assert isinstance(kwargs["mic"], SwitchedMic)
    assert isinstance(kwargs["speaker"], SwitchedPlayer)


def test_the_dashboard_starts_on_its_port_and_stops_on_exit(monkeypatch, fakes, calls):
    monkeypatch.setattr(
        entry, "load_settings", lambda: Settings(anthropic_api_key="k", dashboard_port=9123)
    )
    assert entry.main() == 0
    ((_, app, port),) = [c for c in calls if isinstance(c, tuple) and c[0] == "start_dashboard"]
    assert port == 9123
    assert app.title == "CARL dashboard"
    shutdown = steps(calls)
    assert shutdown.index("farewell") < shutdown.index("dashboard stopped")


def test_the_dashboard_status_and_preview_use_the_running_parts(monkeypatch, fakes, calls):
    monkeypatch.setattr(
        entry,
        "load_settings",
        lambda: Settings(anthropic_api_key="sk-ant-PLANTED", robot_token="t" * 20, max_sentences=3),
    )
    seen = {}

    async def start_and_ask(app, port):
        routes = {
            r.path: r.endpoint for r in app.routes if "GET" in (getattr(r, "methods", None) or ())
        }
        seen["status"] = await routes["/api/status"]()
        seen["personality"] = await routes["/api/personality"]()
        return None

    monkeypatch.setattr(entry, "start_dashboard", start_and_ask)
    entry.main()
    status = seen["status"]
    assert status["state"] == "idle"
    assert status["voice"] == "piper en_GB-northern_english_male-medium"
    assert status["robot"] == {"enabled": True, "connected": False, "fw_version": None}
    assert "sk-ant-PLANTED" not in str(seen)
    assert "at most 3 short sentences" in seen["personality"]["preview"]


def test_a_dashboard_that_cant_start_is_not_fatal(monkeypatch, fakes, calls):
    async def no_dashboard(app, port):
        return None

    monkeypatch.setattr(entry, "start_dashboard", no_dashboard)
    assert entry.main() == 0
    assert "orchestrator shutdown" in steps(calls)


def test_one_event_bus_feeds_the_dashboard(monkeypatch, fakes, calls):
    from desk_buddy.events import EventBus

    monkeypatch.setattr(
        entry, "load_settings", lambda: Settings(anthropic_api_key="k", robot_token=TOKEN)
    )
    entry.main()
    bus = orchestrator_kwargs(calls)["bus"]
    assert isinstance(bus, EventBus)
    (app_kwargs,) = [c[1] for c in calls if isinstance(c, tuple) and c[0] == "create_app"]
    assert app_kwargs["bus"] is bus


def test_dashboard_settings_apply_live_at_the_next_question(monkeypatch, fakes, calls):
    lives, guards, chats = [], [], []

    class SpyLive(entry.LiveSettings):
        def __init__(self, settings):
            super().__init__(settings)
            lives.append(self)

    class SpyGuard(entry.ActionGuard):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            guards.append(self)

    monkeypatch.setattr(entry, "LiveSettings", SpyLive)
    monkeypatch.setattr(entry, "ActionGuard", SpyGuard)
    monkeypatch.setattr(
        entry, "create_chat", lambda s, tools=None, **_: chats.append(s) or object()
    )
    entry.main()
    (live,) = lives
    (guard,) = guards
    kwargs = orchestrator_kwargs(calls)
    conversation = kwargs["conversation"]
    for n in range(5):
        conversation.add(f"q{n}", "[happy] r")
    assert len(chats) == 1

    new = Settings(anthropic_api_key="k", computer_control=True, history_turns=2, tts_speed=1.5)
    live.propose(new)
    assert ("synthesizer speed", 1.5) in calls  # the next sentence
    assert guard.enabled is False  # the rest waits for the next question
    kwargs["on_question"]()
    assert guard.enabled is True
    assert len(conversation) == 2
    assert len(chats) == 2 and chats[-1] is new  # tools and chat rebuilt


def test_a_settings_change_that_doesnt_touch_the_chat_doesnt_rebuild_it(monkeypatch, fakes, calls):
    lives, chats = [], []

    class SpyLive(entry.LiveSettings):
        def __init__(self, settings):
            super().__init__(settings)
            lives.append(self)

    monkeypatch.setattr(entry, "LiveSettings", SpyLive)
    monkeypatch.setattr(
        entry, "create_chat", lambda s, tools=None, **_: chats.append(s) or object()
    )
    entry.main()
    lives[0].propose(Settings(anthropic_api_key="k", history_turns=3))
    orchestrator_kwargs(calls)["on_question"]()
    assert len(chats) == 1


@pytest.mark.parametrize(
    "change",
    [{"web_search": False}, {"web_search_max": 5}, {"smart_model": ""}],
    ids=["switch", "max", "smart_model"],
)
def test_web_search_settings_rebuild_the_chat_at_the_next_question(
    monkeypatch, fakes, calls, change
):
    lives, chats = [], []

    class SpyLive(entry.LiveSettings):
        def __init__(self, settings):
            super().__init__(settings)
            lives.append(self)

    monkeypatch.setattr(entry, "LiveSettings", SpyLive)
    monkeypatch.setattr(
        entry, "create_chat", lambda s, tools=None, **_: chats.append(s) or object()
    )
    entry.main()
    new = Settings(anthropic_api_key="k", **change)
    lives[0].propose(new)
    assert len(chats) == 1  # not mid-question
    orchestrator_kwargs(calls)["on_question"]()
    assert len(chats) == 2 and chats[-1] is new


def apps_run(monkeypatch, tmp_path, **settings):
    """main() with a spy on the apps store and on every tool registry the chat gets."""
    import json

    (tmp_path / "desk-buddy").mkdir(exist_ok=True)
    (tmp_path / "desk-buddy" / "bookmarks.json").write_text(
        json.dumps([{"name": "weather", "url": "https://example.com/weather"}]), encoding="utf-8"
    )
    stores, registries = [], []

    class SpyStore(entry.AppsStore):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            stores.append(self)

    monkeypatch.setattr(entry, "AppsStore", SpyStore)
    monkeypatch.setattr(entry, "load_settings", lambda: Settings(anthropic_api_key="k", **settings))
    monkeypatch.setattr(
        entry, "create_chat", lambda s, tools=None, **_: registries.append(tools) or object()
    )
    entry.main()
    (store,) = stores
    return store, registries


def test_bookmarks_and_apps_become_tools_when_computer_control_is_on(
    monkeypatch, fakes, calls, tmp_path, caplog
):
    caplog.set_level(logging.INFO)
    store, registries = apps_run(monkeypatch, tmp_path, computer_control=True)
    assert store.apps_file == tmp_path / "desk-buddy" / "apps.json"  # never the real one
    assert "open_website" in registries[0] and "open_app" not in registries[0]
    assert "Apps and websites: on (0 apps, 1 websites)" in caplog.messages


def test_search_results_can_be_opened_with_computer_control_and_web_search(
    monkeypatch, fakes, calls, tmp_path, caplog
):
    caplog.set_level(logging.INFO)
    _, registries = apps_run(monkeypatch, tmp_path, computer_control=True)
    assert "open_search_result" in registries[0]
    assert "Opening search results: on (after a spoken yes)" in caplog.messages
    kwargs = orchestrator_kwargs(calls)
    assert kwargs["confirmations"] is not None and kwargs["search_results"] is not None


def test_no_search_results_to_open_without_web_search(monkeypatch, fakes, calls, tmp_path):
    _, registries = apps_run(monkeypatch, tmp_path, computer_control=True, web_search=False)
    assert "open_search_result" not in registries[0]
    assert "open_website" in registries[0]


def test_the_pending_yes_survives_rebuilds_and_expires_when_computer_control_goes_off(monkeypatch):
    from desk_buddy.tools.confirm import PendingAction

    seen = []
    monkeypatch.setattr(
        entry, "create_chat", lambda *a, search_results=None, **k: seen.append(search_results)
    )
    on = Settings(anthropic_api_key="k", computer_control=True)
    off = Settings(anthropic_api_key="k", computer_control=False)
    brain = entry.Brain(on, bus=entry.EventBus())
    confirmations, results = brain.confirmations, brain.search_results
    brain.on_settings(on, Settings(anthropic_api_key="k", computer_control=True, max_sentences=5))
    assert brain.confirmations is confirmations and brain.search_results is results
    assert seen == [results, results]  # every chat feeds the same results
    ran = []
    confirmations.propose(PendingAction("t", "{}", "{}", "x", lambda: ran.append(1) or "ok"))
    brain.on_settings_now(on, off)
    assert confirmations.pending is None
    note = confirmations.answer("What's next?", 2)
    assert note == (
        "(System note: the request to open x was dropped because computer control was "
        "turned off; nothing was opened.)"
    )
    assert ran == []


def test_apps_and_websites_are_off_without_computer_control(
    monkeypatch, fakes, calls, tmp_path, caplog
):
    caplog.set_level(logging.INFO)
    store, registries = apps_run(monkeypatch, tmp_path)
    assert "open_website" not in registries[0]
    assert "Apps and websites: off (COMPUTER_CONTROL is off)" in caplog.messages
    store.update_bookmarks([{"name": "news", "url": "https://news.example"}])
    orchestrator_kwargs(calls)["on_question"]()
    assert "open_website" not in registries[-1]


def test_a_changed_list_rebuilds_the_tools_at_the_next_question_only(
    monkeypatch, fakes, calls, tmp_path
):
    store, registries = apps_run(monkeypatch, tmp_path, computer_control=True)
    on_question = orchestrator_kwargs(calls)["on_question"]
    on_question()
    assert len(registries) == 1  # nothing changed: no rebuild
    assert store.update_bookmarks([{"name": "news", "url": "https://news.example"}]) == {}
    assert len(registries) == 1  # not mid-question
    on_question()
    assert len(registries) == 2
    schema = next(s for s in registries[-1].schemas() if s["name"] == "open_website")
    assert schema["input_schema"]["properties"]["name"]["enum"] == ["news"]
    on_question()
    assert len(registries) == 2  # once per change
    assert store.update_bookmarks([]) == {}
    on_question()
    assert "open_website" not in registries[-1]


def test_an_approval_change_rebuilds_the_tools_at_the_next_question_only(monkeypatch, tmp_path):
    from desk_buddy.skills_store import SkillsStore

    built = []
    monkeypatch.setattr(entry, "create_chat", lambda *a, **k: built.append(1) or object())
    store = SkillsStore(tmp_path / "skills.json")
    brain = entry.Brain(Settings(anthropic_api_key="k"), bus=entry.EventBus(), skills=store)
    assert brain.skills is store and brain.skill_runner is None
    brain.before_question()
    assert len(built) == 1  # nothing changed
    store.approve("hello-time", "a" * 64)
    assert len(built) == 1  # not mid-question
    brain.before_question()
    assert len(built) == 2
    brain.before_question()
    assert len(built) == 2  # once per change
    store.disable("hello-time")
    brain.before_question()
    assert len(built) == 3


def test_restored_files_bring_a_skill_back_to_carl(monkeypatch, tmp_path):
    from desk_buddy.skills_setup import approve
    from desk_buddy.skills_store import SkillsStore
    from desk_buddy.tools.skills import SkillRunner

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    root = tmp_path / "skills"
    folder = root / "hello-time"
    folder.mkdir(parents=True)
    (folder / "skill.json").write_text(
        json.dumps({"name": "Hello", "spoken_names": ["hello"], "description": "Hi."}),
        encoding="utf-8",
    )
    (folder / "main.py").write_text("print('hi')\n", encoding="utf-8")
    registries = []
    monkeypatch.setattr(
        entry, "create_chat", lambda s, tools=None, **_: registries.append(tools) or object()
    )
    store = SkillsStore(tmp_path / "skills.json")
    runner = SkillRunner(r"C:\Tools\uv.exe", tmp_path / "appdata")
    settings = Settings(anthropic_api_key="k", computer_control=True, skills_dir=str(root))
    lock = threading.Lock()

    def tick():
        return approve(store, runner, str(root), "hello-time", frozenset, lock=lock)

    assert tick().status == 200
    brain = entry.Brain(settings, bus=entry.EventBus(), skills=store, skill_runner=runner)

    def offered():
        return "skill_hello_time" in [s["name"] for s in registries[-1].schemas()]

    assert offered()
    (folder / "main.py").write_text("print('changed')\n", encoding="utf-8")
    store.touch()  # any rescan while it's changed (say, another skill ticked)...
    brain.before_question()
    assert not offered()  # ...caches a list without it
    (folder / "main.py").write_text("print('hi')\n", encoding="utf-8")  # restored
    # The next rebuild looks for the approved skill missing from the cached list.
    brain._rebuild()
    assert offered()
    # And ticking it again (it's already on) is a rescan, not a refusal.
    version = store.version
    assert tick().status == 200
    assert store.version == version + 1
    built = len(registries)
    brain.before_question()
    assert len(registries) == built + 1 and offered()


def test_the_brain_and_the_dashboard_share_one_skills_store_and_runner(
    monkeypatch, fakes, calls, tmp_path, caplog
):
    caplog.set_level(logging.INFO)
    uv = tmp_path / "bin" / "uv.exe"
    uv.parent.mkdir()
    uv.write_bytes(b"")
    monkeypatch.setenv("PATH", str(uv.parent))
    made = {}
    real = entry.create_dashboard

    def spy_dashboard(**kwargs):
        made.update(kwargs)
        return real(**kwargs)

    brains = []

    class SpyBrain(entry.Brain):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            brains.append(self)

    monkeypatch.setattr(entry, "create_dashboard", spy_dashboard)
    monkeypatch.setattr(entry, "Brain", SpyBrain)
    monkeypatch.setattr(entry, "load_settings", lambda: Settings(anthropic_api_key="k"))
    entry.main()
    (brain,) = brains
    assert made["skills"] is brain.skills is not None
    assert made["skill_runner"] is brain.skill_runner is not None
    assert brain.skill_runner.uv == str(uv)
    assert brain.skill_runner.app_dir == tmp_path / "desk-buddy"  # never the real one
    assert brain.skills.approvals_file == tmp_path / "desk-buddy" / "skills.json"


def test_shutdown_kills_a_running_setup_before_the_dashboard_stops(
    monkeypatch, fakes, calls, tmp_path
):
    uv = tmp_path / "bin" / "uv.exe"
    uv.parent.mkdir()
    uv.write_bytes(b"")
    monkeypatch.setenv("PATH", str(uv.parent))
    monkeypatch.setattr(entry.SkillRunner, "stop", lambda self: calls.append("skills stopped"))
    monkeypatch.setattr(entry, "load_settings", lambda: Settings(anthropic_api_key="k"))
    entry.main()
    # A `uv sync` in progress is killed first, so the dashboard's request (and the
    # worker thread) never hold up shutdown; the brain stops it again on close.
    assert calls.index("skills stopped") < calls.index("dashboard stopped")
    assert calls.count("skills stopped") == 2


def test_closing_the_brain_stops_the_skill_runner():
    stopped = []

    class Runner:
        def stop(self):
            stopped.append(1)

    brain = entry.Brain(
        Settings(anthropic_api_key="k"), bus=entry.EventBus(), skill_runner=Runner()
    )
    brain.close()
    assert stopped == [1]


def test_without_uv_skills_are_off_with_a_warning(monkeypatch, fakes, calls, tmp_path, caplog):
    caplog.set_level(logging.INFO)
    monkeypatch.setenv("PATH", "")
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    brains = []

    class SpyBrain(entry.Brain):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            brains.append(self)

    monkeypatch.setattr(entry, "Brain", SpyBrain)
    monkeypatch.setattr(
        entry,
        "load_settings",
        lambda: Settings(anthropic_api_key="k", skills_dir=str(skills_dir)),
    )
    entry.main()
    assert brains[0].skill_runner is None
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings.count("Skills: off (uv.exe wasn't found on PATH)") == 1


def orchestrator_kwargs(calls):
    (kwargs,) = [c[1] for c in calls if isinstance(c, tuple) and c[0] == "orchestrator kwargs"]
    return kwargs


def music_run(monkeypatch, calls, caplog, control, **settings):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(entry, "load_settings", lambda: Settings(anthropic_api_key="k", **settings))
    made = []

    def fake_create_control(client_id, **kwargs):
        made.append((client_id, kwargs))
        return control

    monkeypatch.setattr(entry, "create_control", fake_create_control)
    tools_seen = []
    monkeypatch.setattr(
        entry, "create_chat", lambda s, tools=None, **_: tools_seen.append(tools) or object()
    )
    assert entry.main() == 0
    return made, tools_seen[0]


def test_the_personality_file_feeds_the_chat_and_the_loop(monkeypatch, fakes, calls, tmp_path):
    import json

    (tmp_path / "desk-buddy").mkdir(exist_ok=True)
    (tmp_path / "desk-buddy" / "personality.json").write_text(
        json.dumps({"character": "You are grumpy.", "lines": {"greeting": {"text": "Hmph."}}}),
        encoding="utf-8",
    )
    providers = []
    monkeypatch.setattr(
        entry,
        "create_chat",
        lambda s, tools=None, personality=None, **_: providers.append(personality) or object(),
    )
    entry.main()
    assert providers[0]().character == "You are grumpy."
    assert orchestrator_kwargs(calls)["personality"]().line("greeting").text == "Hmph."


@pytest.mark.parametrize(
    ("settings", "reason"),
    [
        ({}, "off (COMPUTER_CONTROL is off)"),
        ({"computer_control": True}, "off (no SPOTIFY_CLIENT_ID)"),
        (
            {"computer_control": True, "spotify_client_id": "cid"},
            "off (not logged in: run uv run python -m desk_buddy.spotify_login)",
        ),
    ],
)
def test_music_tools_are_off_unless_switched_on_and_logged_in(
    monkeypatch, fakes, calls, caplog, settings, reason
):
    made, tools = music_run(monkeypatch, calls, caplog, None, **settings)
    assert f"Music tools: {reason}" in caplog.messages
    assert "play_music" not in tools
    if not settings.get("spotify_client_id"):
        assert made == []  # nothing Spotify is touched while it's off


def test_music_tools_are_on_with_a_login(monkeypatch, fakes, calls, caplog):
    from desk_buddy.tools.spotify import SpotifyControl

    questions = []

    class SpyGuard(entry.ActionGuard):
        def new_question(self):
            questions.append(1)
            super().new_question()

    monkeypatch.setattr(entry, "ActionGuard", SpyGuard)
    control = SpotifyControl(object(), pc_name="pc")
    made, tools = music_run(
        monkeypatch,
        calls,
        caplog,
        control,
        computer_control=True,
        spotify_client_id="cid",
        music_duck=False,
    )
    assert made[0] == ("cid", {})  # (the ducker is made too, switched off, for later)
    assert "play_music" in tools
    assert "Music tools: on (Spotify)" in caplog.messages
    # Each turn starts a new question for the action limits.
    orchestrator_kwargs(calls)["on_question"]()
    assert questions == [1]


def test_music_ducking_follows_the_state_with_its_own_quick_client(
    monkeypatch, fakes, calls, caplog
):
    from desk_buddy.loop import State
    from desk_buddy.tools.ducking import DUCK_TIMEOUT_S
    from desk_buddy.tools.spotify import SpotifyControl

    ducked = []

    class Ducker:
        def __init__(self, control, *, enabled=True):
            calls.append("ducker created")

        def on_state(self, state):
            ducked.append(state)

        def set_enabled(self, enabled, state="idle"):
            calls.append(("ducker enabled", enabled))

        def close(self):
            calls.append("ducker closed")

    monkeypatch.setattr(entry, "MusicDucker", Ducker)
    control = SpotifyControl(object(), pc_name="pc")
    made, _ = music_run(
        monkeypatch, calls, caplog, control, computer_control=True, spotify_client_id="cid"
    )
    assert made == [("cid", {}), ("cid", {"timeout": DUCK_TIMEOUT_S, "retries": 0})]
    assert "Music tools: on (Spotify, ducking)" in caplog.messages
    orchestrator_kwargs(calls)["on_state"](State.LISTENING)
    assert ducked == [State.LISTENING]
    shutdown = steps(calls)
    assert shutdown.index("farewell") < shutdown.index("ducker closed")


class VolumeSpotify:
    """Just enough Spotify for ducking: a playing device with a volume."""

    def __init__(self, volume):
        self.volume_percent = volume

    def current_playback(self, market=None, additional_types=None):
        return {"is_playing": True, "device": {"id": "pc", "volume_percent": self.volume_percent}}

    def volume(self, volume_percent, device_id=None):
        self.volume_percent = volume_percent


def test_changing_music_duck_mid_question_leaves_the_music_as_it_was(monkeypatch):
    from desk_buddy.events import EventBus
    from desk_buddy.loop import State
    from desk_buddy.tools.ducking import MusicDucker
    from desk_buddy.tools.spotify import MusicTools, SpotifyControl

    monkeypatch.setattr(entry, "create_chat", lambda *a, **k: object())
    client = VolumeSpotify(70)
    control = SpotifyControl(client, pc_name="pc")
    ducker = MusicDucker(control)
    guard = entry.ActionGuard(True)
    on = Settings(anthropic_api_key="k", computer_control=True, music_duck=True)
    off = Settings(anthropic_api_key="k", computer_control=True, music_duck=False)
    brain = entry.Brain(
        on, bus=EventBus(), guard=guard, music=(MusicTools(control, guard, ducker=ducker), ducker)
    )
    brain.on_state(State.LISTENING)
    assert ducker.flush(2) and client.volume_percent == 14
    brain.on_settings(on, off)  # applied at the next question, mid-way through this one
    brain.on_state(State.THINKING)
    brain.on_state(State.SPEAKING)
    brain.on_state(State.IDLE)
    assert ducker.flush(2)
    assert client.volume_percent == 70
    assert brain.ducker is ducker  # switched, never replaced
    brain.on_settings(off, on)  # and back on, mid-question again
    brain.on_state(State.LISTENING)
    brain.on_state(State.IDLE)
    assert ducker.flush(2)
    assert client.volume_percent == 70
    brain.close()


def test_turning_computer_control_off_applies_at_once_and_on_waits(monkeypatch, fakes, calls):
    lives, guards, apps = [], [], []

    class SpyLive(entry.LiveSettings):
        def __init__(self, settings):
            super().__init__(settings)
            lives.append(self)

    class SpyGuard(entry.ActionGuard):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            guards.append(self)

    async def keep_app(app, port):
        apps.append(app)
        return None

    monkeypatch.setattr(entry, "LiveSettings", SpyLive)
    monkeypatch.setattr(entry, "ActionGuard", SpyGuard)
    monkeypatch.setattr(entry, "start_dashboard", keep_app)
    monkeypatch.setattr(
        entry, "load_settings", lambda: Settings(anthropic_api_key="k", computer_control=True)
    )
    entry.main()
    (live,), (guard,), (app,) = lives, guards, apps
    status = next(r.endpoint for r in app.routes if getattr(r, "path", "") == "/api/status")

    live.propose(Settings(anthropic_api_key="k", computer_control=False))
    assert guard.enabled is False  # at once, mid-question
    assert asyncio.run(status())["computer_control"] is False

    live.propose(Settings(anthropic_api_key="k", computer_control=True))
    assert guard.enabled is False  # turning on waits for the next question
    orchestrator_kwargs(calls)["on_question"]()
    assert guard.enabled is True


def test_a_dashboard_stt_value_that_wont_load_falls_back_once(monkeypatch, fakes, calls, caplog):
    caplog.set_level(logging.INFO)
    loaded = []

    class PickyTranscriber:
        def __init__(self, model, **kwargs):
            loaded.append(model)
            if model == "large-v9":
                raise RuntimeError("no such model")
            calls.append("transcriber created")

    monkeypatch.setattr(entry, "WhisperTranscriber", PickyTranscriber)
    monkeypatch.setattr(
        entry, "load_settings", lambda: Settings(anthropic_api_key="k", stt_model="large-v9")
    )
    monkeypatch.setattr(entry, "_settings_without", lambda keys: Settings(anthropic_api_key="k"))
    assert entry.main() == 0
    assert loaded == ["large-v9", "base.en"]
    assert "didn't load with the dashboard's settings" in caplog.text


def test_a_dashboard_voice_that_wont_load_falls_back_once(monkeypatch, fakes, calls, caplog):
    caplog.set_level(logging.INFO)
    tried = []

    def picky_load_piper(voice, voices_dir, *, speed):
        tried.append(voice)
        if voice == "en_US-nope-medium":
            raise VoiceError("Couldn't get voice 'en_US-nope-medium': 404")
        calls.append("synthesizer created")

        class Synth:
            def close(self):
                calls.append("synthesizer closed")

        return Synth()

    monkeypatch.setattr(entry, "load_piper", picky_load_piper)
    monkeypatch.setattr(
        entry,
        "load_settings",
        lambda: Settings(anthropic_api_key="k", tts_voice="en_US-nope-medium"),
    )
    monkeypatch.setattr(entry, "_settings_without", lambda keys: Settings(anthropic_api_key="k"))
    assert entry.main() == 0
    assert tried == ["en_US-nope-medium", "en_GB-northern_english_male-medium"]
    assert "Voice: piper en_GB-northern_english_male-medium" in caplog.messages


def test_settings_without_only_falls_back_from_what_the_dashboard_set(monkeypatch, tmp_path):
    import json

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert entry._settings_without(("STT_MODEL",)) is None  # no file: nothing to fall back from
    (tmp_path / "desk-buddy").mkdir()
    (tmp_path / "desk-buddy" / "settings.json").write_text(
        json.dumps({"MAX_SENTENCES": "4"}), encoding="utf-8"
    )
    assert entry._settings_without(("STT_MODEL",)) is None  # it didn't set STT_MODEL


def test_busy_port_is_a_clear_startup_error(monkeypatch, capsys, fakes, calls):
    monkeypatch.setattr(
        entry, "load_settings", lambda: Settings(anthropic_api_key="k", robot_token=TOKEN)
    )

    async def busy(app, host, port):
        raise ServerStartError("Can't listen on 127.0.0.1:8765: in use")

    monkeypatch.setattr(entry, "start_server", busy)

    assert entry.main() == 2

    err = capsys.readouterr().err
    assert "Can't start: Can't listen on 127.0.0.1:8765: in use" in err
    assert "Traceback" not in err
    assert steps(calls) == [
        "transcriber created",
        "synthesizer created",
        "mic created",
        "speaker created",
        "orchestrator shutdown",
        "speaker closed",
        "mic closed",
        "synthesizer closed",
    ]


class Device:
    def start(self):
        pass

    def stop(self):
        return b""


class RecordingRobotMic(Device):
    def __init__(self, log):
        self.log = log

    def begin(self):
        self.log.append("begin")

    def end(self):
        self.log.append("end")


class KeyRobot:
    """A robot whose mic records begin/end into a shared log."""

    def __init__(self, log):
        self.mic = RecordingRobotMic(log)
        self.player = None

    def send(self, item):
        pass


DELAY = 0.05


def drain(events):
    out = []
    while not events.empty():
        out.append(events.get_nowait())
    return out


class LoggingQueue(asyncio.Queue):
    """Records each queued event in the same log as the mic calls, in call order."""

    def __init__(self, log):
        super().__init__()
        self.log = log

    def put_nowait(self, item):
        self.log.append(item)
        super().put_nowait(item)


def key_setup(log):
    switch = DeviceSwitch(Device(), Device())
    events = LoggingQueue(log)
    return switch, events, entry.key_handler(switch, events, delay=DELAY)


async def until(condition, timeout=2.0):
    """Poll until `condition()` is true, or fail after `timeout` seconds."""
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.005)


def test_the_key_goes_straight_to_the_events_without_a_robot():
    async def run():
        switch = DeviceSwitch(Device(), Device())
        events: asyncio.Queue[str] = asyncio.Queue()
        on_key = entry.key_handler(switch, events, delay=DELAY)
        on_key("down")
        on_key("up")
        return drain(events)

    assert asyncio.run(run()) == ["down", "up"]


def test_key_down_with_a_robot_begins_its_mic_then_queues_down():
    log = []

    async def run():
        switch, events, press = key_setup(log)
        switch.attach(KeyRobot(log))
        press("down")

    asyncio.run(run())
    assert log == ["begin", "down"]


def test_key_up_with_a_robot_ends_its_mic_after_the_delay():
    log = []

    async def run():
        switch, events, press = key_setup(log)
        switch.attach(KeyRobot(log))
        press("down")
        press("up")
        assert log == ["begin", "down"]  # nothing yet: in-flight audio may still arrive
        await until(lambda: "up" in log)
        assert drain(events) == ["down", "up"]

    asyncio.run(run())
    assert log == ["begin", "down", "end", "up"]


def test_a_re_press_during_the_delay_releases_first():
    log = []

    async def run():
        switch, events, press = key_setup(log)
        switch.attach(KeyRobot(log))
        press("down")
        press("up")
        press("down")
        assert log == ["begin", "down", "end", "up", "begin", "down"]
        # Release again and wait for that release. Had the cancelled first release also
        # fired (it was due earlier), an extra "up" would show up before this one.
        press("up")
        await until(lambda: log.count("up") >= 2)

    asyncio.run(run())
    assert log == ["begin", "down", "end", "up", "begin", "down", "end", "up"]


def test_a_double_press_within_the_delay_drops_the_first_clip():
    """Deliberate: a re-press within the delay ends and discards clip 1 (the robot mic's
    begin() clears its buffer before the orchestrator takes it), so it counts as a tap."""
    from desk_buddy.link.robot import RobotMic

    class Robot:
        def __init__(self):
            self.mic = RobotMic()
            self.player = None

        def send(self, item):
            pass

    robot = Robot()

    async def run():
        switch = DeviceSwitch(Device(), Device())
        events: asyncio.Queue[str] = asyncio.Queue()
        on_key = entry.key_handler(switch, events, delay=DELAY)
        switch.attach(robot)
        on_key("down")
        robot.mic.feed(b"\x01\x00" * 100)  # clip 1
        on_key("up")
        on_key("down")  # within the delay
        # The orchestrator handles the queued events (it runs on the same loop).
        assert drain(events) == ["down", "up", "down"]
        first = robot.mic.stop()  # its stop() on that "up"
        robot.mic.feed(b"\x02\x00" * 100)  # clip 2
        on_key("up")
        await until(lambda: not events.empty())
        assert drain(events) == ["up"]
        return first, robot.mic.stop()

    first, second = asyncio.run(run())
    assert first == b""  # clip 1 was cleared by the re-press: an ignored tap
    assert second == b"\x02\x00" * 100


def test_a_robot_detaching_during_the_delay_still_queues_up():
    log = []

    async def run():
        switch, events, press = key_setup(log)
        robot = KeyRobot(log)
        switch.attach(robot)
        press("down")
        press("up")
        switch.detach(robot)
        await until(lambda: "up" in log)

    asyncio.run(run())
    assert log == ["begin", "down", "up"]  # no end() on the old robot


def test_the_key_with_a_robot_attached_mid_press():
    log = []

    async def run():
        switch, events, press = key_setup(log)
        press("down")  # no robot yet: straight through
        switch.attach(KeyRobot(log))
        press("up")
        await until(lambda: "up" in log)

    asyncio.run(run())
    assert log == ["down", "end", "up"]


# --- the dashboard's controls ---


def test_the_dashboards_talk_goes_through_the_key_handler_with_a_robot():
    log = []

    async def run():
        switch, events, on_key = key_setup(log)
        switch.attach(KeyRobot(log))
        control = entry.dashboard_control(on_key, events)
        control("down")
        control("up")
        assert log == ["begin", "down"]  # the release delay applies, as for the key
        await until(lambda: "up" in log)

    asyncio.run(run())
    assert log == ["begin", "down", "end", "up"]


def test_the_dashboards_stop_and_forget_go_onto_the_queue():
    log = []

    async def run():
        switch, events, on_key = key_setup(log)
        switch.attach(KeyRobot(log))
        control = entry.dashboard_control(on_key, events)
        control("stop")
        control("forget")
        return drain(events)

    assert asyncio.run(run()) == ["stop", "forget"]
    assert log == ["stop", "forget"]  # never through the key handler: the mic is untouched


def test_the_dashboards_control_refuses_anything_else():
    async def run():
        switch, events, on_key = key_setup([])
        control = entry.dashboard_control(on_key, events)
        with pytest.raises(ValueError):
            control("reset")
        return drain(events)

    assert asyncio.run(run()) == []


def test_main_gives_the_dashboard_its_control(monkeypatch, fakes, calls):
    made = {}
    real = entry.create_dashboard

    def spy_dashboard(**kwargs):
        made.update(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(entry, "create_dashboard", spy_dashboard)
    entry.main()
    assert callable(made["control"])
