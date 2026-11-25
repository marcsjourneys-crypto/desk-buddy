"""Run the voice loop on the PC: uv run python -m desk_buddy"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import secrets
import socket
import sys
import threading
from collections.abc import Callable
from typing import Any

from desk_buddy.apps_store import AppsStore
from desk_buddy.audio import AudioDeviceError, Microphone, Speaker, check_audio_devices
from desk_buddy.brain.memory import Conversation
from desk_buddy.brain.personality import Personality
from desk_buddy.config import (
    ConfigError,
    Settings,
    dashboard_settings_path,
    load_settings,
    read_dashboard_values,
)
from desk_buddy.dashboard import build_status, create_dashboard, start_dashboard
from desk_buddy.events import EventBus
from desk_buddy.link.switch import DeviceSwitch, Robot
from desk_buddy.llm import create_chat, prompt_for, web_search_blocked
from desk_buddy.loop import Orchestrator, State
from desk_buddy.personality_store import PersonalityStore
from desk_buddy.ptt import KeyWatcher, parse_key
from desk_buddy.server import ServerStartError, create_app, start_server
from desk_buddy.settings_store import LiveSettings, SettingsStore
from desk_buddy.stt import WhisperTranscriber
from desk_buddy.tools import build_tools
from desk_buddy.tools.actions import ActionGuard
from desk_buddy.tools.confirm import Confirmations
from desk_buddy.tools.ducking import DUCK_TIMEOUT_S, MusicDucker
from desk_buddy.tools.launch import LaunchTools
from desk_buddy.tools.media import MediaKeys
from desk_buddy.tools.spotify import MusicTools, create_control
from desk_buddy.tools.urls import SearchResults
from desk_buddy.tools.youtube import YouTubeClient, YouTubeTools
from desk_buddy.tts import PiperSynthesizer, SapiSynthesizer, VoiceError, load_piper

log = logging.getLogger("desk_buddy")

FAREWELL_TIMEOUT = 5.0  # seconds the farewell may play before it is cut off...
FAREWELL_HARD_CAP = 6.0  # ...and the most it may ever delay shutdown
RELEASE_DELAY = 0.25  # seconds the robot's mic stays open after the key is released
WILDCARD_HOSTS = ("", "0.0.0.0", "::")  # listening on every interface: the robot needs the LAN IP


class StartupError(Exception):
    """A part of the voice loop couldn't be built. The message says which and why."""


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        settings = load_settings()
        check_audio_devices()
        asyncio.run(_run(settings))
    except (ConfigError, AudioDeviceError, StartupError) as exc:
        # Only raised while starting up: the mic/speaker streams open in their constructors.
        print(f"Can't start: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        pass  # CARL already said its farewell
    return 0


async def _run(settings: Settings) -> None:
    # The exit stack closes whatever was already built, in reverse order, however we leave:
    # a failed startup, Ctrl+C, or an error in the loop.
    async with contextlib.AsyncExitStack() as stack:
        log.info(
            "Loading speech-to-text model %s. The first run downloads about 150 MB; "
            "Ctrl+C waits until the download finishes.",
            settings.stt_model,
        )
        try:
            transcriber = await _load_whisper(settings)
        except Exception as exc:
            # A dashboard value may be what broke it: try once more without it.
            fallback = _settings_without(_STT_KEYS)
            if fallback is None:
                raise _whisper_error(settings, exc) from exc
            log.warning(
                "Speech-to-text didn't load with the dashboard's settings (%s); "
                "using .env and defaults for it this time",
                exc,
            )
            settings = dataclasses.replace(
                settings, **{key.lower(): getattr(fallback, key.lower()) for key in _STT_KEYS}
            )
            try:
                transcriber = await _load_whisper(settings)
            except Exception as exc2:
                raise _whisper_error(settings, exc2) from exc2
        synthesizer, settings = await _make_synthesizer(settings)
        stack.callback(synthesizer.close)
        mic = Microphone()
        stack.callback(mic.close)
        speaker = Speaker()
        stack.callback(speaker.close)
        bus = EventBus()  # live events for the dashboard; never blocks the voice loop
        personality = PersonalityStore()  # edited on the dashboard; defaults otherwise
        apps = AppsStore()  # the apps and websites CARL may open, from the Apps tab
        guard = ActionGuard(settings.computer_control, bus=bus)
        # Reads the Spotify login from disk: off the event loop.
        music = await asyncio.to_thread(_music_tools, settings, guard)
        brain = Brain(
            settings,
            bus=bus,
            personality=personality.current,
            guard=guard,
            music=music,
            apps=apps,
        )
        stack.callback(brain.close)  # runs after the farewell, which restores the music
        conversation = Conversation(settings.history_turns)
        # A connected robot takes over the mic, speaker and button; else the PC's are used.
        switch = DeviceSwitch(mic, speaker)
        live = LiveSettings(settings)

        def on_question() -> None:
            live.apply()  # dashboard changes land between turns, never during one
            brain.before_question()
            orchestrator.chat = brain.chat
            brain.guard.new_question()

        def on_state(state: State) -> None:
            switch.send_state(state)
            brain.on_state(state)  # ducking; never blocks or raises

        orchestrator = Orchestrator(
            mic=switch.mic,
            transcriber=transcriber,
            chat=brain.chat,
            synthesizer=synthesizer,
            speaker=switch.player,
            conversation=conversation,
            on_state=on_state,
            on_emotion=switch.send_emotion,
            on_question=on_question,
            bus=bus,
            personality=personality.current,
            confirmations=brain.confirmations,
            search_results=brain.search_results,
        )
        stack.push_async_callback(orchestrator.shutdown)
        live.on_question(brain.on_settings)
        live.on_question(lambda old, new: conversation.set_max_turns(new.history_turns))
        live.on_now(lambda old, new: _set_speed(synthesizer, new.tts_speed))
        live.on_now(brain.on_settings_now)  # computer control off: at once

        events: asyncio.Queue[str] = asyncio.Queue()
        if settings.robot_token:
            app = create_app(
                token=settings.robot_token,
                switch=switch,
                events=events,
                current_state=lambda: orchestrator.state.value,
                # Queued after the connect's "reset", so the greeting follows it.
                on_connect=lambda: events.put_nowait("greet"),
                bus=bus,
            )
            try:
                server = await start_server(app, settings.server_host, settings.server_port)
            except ServerStartError as exc:
                raise StartupError(str(exc)) from exc
            stack.push_async_callback(server.stop)
            port = settings.server_port
            ip = lan_ip() if settings.server_host in WILDCARD_HOSTS else None
            if ip is not None:
                log.info(
                    "Robot link: on (ws://%s:%d/robot — set the robot with: server %s:%d)",
                    ip,
                    port,
                    ip,
                    port,
                )
            else:
                log.info("Robot link: on (ws://%s:%d/robot)", settings.server_host, port)
        else:
            log.info("Robot link: off (no ROBOT_TOKEN)")

        voice = "sapi" if settings.tts_engine == "sapi" else f"piper {settings.tts_voice}"
        dashboard_app = create_dashboard(
            port=settings.dashboard_port,
            token=secrets.token_urlsafe(32),  # put into the page: changes must carry it
            settings=SettingsStore(),
            live=live,
            personality=personality,
            bus=bus,
            status=lambda: build_status(
                live.current,
                voice=voice,
                tools=[s["name"] for s in brain.tools.schemas()],
                robot=switch.robot,
                state=orchestrator.state.value,
                dashboard_port=settings.dashboard_port,
                computer_control=brain.guard.enabled,  # what's in force right now
            ),
            preview=lambda: prompt_for(live.current, brain.tools, personality.current()),
            apps=apps,
        )
        dashboard = await start_dashboard(dashboard_app, settings.dashboard_port)
        if dashboard is not None:  # never fatal: CARL runs without it
            stack.push_async_callback(dashboard.stop)

        loop = asyncio.get_running_loop()
        on_key = key_handler(switch, events)
        watcher = KeyWatcher(
            parse_key(settings.ptt_key),
            lambda event: loop.call_soon_threadsafe(on_key, event),
        )
        watcher.start()
        stack.callback(watcher.stop)
        # Last in, so first out: the farewell plays before the server and devices close.
        stack.push_async_callback(_say_farewell, orchestrator)
        log.info("Ready. Hold %s to talk, Ctrl+C to quit.", settings.ptt_key)
        events.put_nowait("greet")
        await orchestrator.run(events)


# Settings whose change means new tools and a new chat (prompt, model, effort, tools).
_CHAT_SETTINGS = (
    "computer_control",
    "max_sentences",
    "llm_model",
    "llm_effort",
    "obsidian_vault_path",
    "obsidian_daily_folder",
    "obsidian_ideas_folder",
    "web_search",
    "web_search_max",
)
_STT_KEYS = ("STT_MODEL", "STT_DEVICE", "STT_COMPUTE_TYPE")


class Brain:
    """What a turn uses that a live setting can change: the action guard, the music tools
    and their ducker, the tool registry and the chat.

    - The tools and chat are rebuilt between turns (LiveSettings.apply at the start of a
      question), never during one.
    - The music tools and their ducker are made once, the first time computer control is
      on with a Spotify login, then kept: turning computer control off just leaves the
      music tools out, and MUSIC_DUCK just switches the ducker. A ducker is never
      replaced, so no handover can leave the music low.
    - Making them later (computer control turned on from the dashboard) reads the
      Spotify login on a worker thread; they join at the next question.
    - The app and website tools are made from a snapshot of the Apps tab's lists at each
      rebuild; a saved change to the lists rebuilds at the next question.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        bus: EventBus,
        personality: Callable[[], Personality] = Personality,
        guard: ActionGuard | None = None,
        music: tuple[MusicTools | None, MusicDucker | None] = (None, None),
        apps: AppsStore | None = None,
    ) -> None:
        self.guard = guard or ActionGuard(settings.computer_control, bus=bus)
        # Kept across rebuilds: the action waiting for a spoken yes, and the search
        # result addresses open_search_result may open (the loop moves them along).
        self.confirmations = Confirmations(bus=bus)
        self.search_results = SearchResults()
        self._app_store = apps
        self._apps_version: int | None = None  # the lists' version the tools were built from
        self._bus = bus  # each web search is shown on the dashboard
        self._personality = personality
        self._settings = settings
        self._state = State.IDLE
        self._music, self.ducker = music
        self._building = False
        self._rebuild_due = False
        self._switch_ducker()
        self._rebuild()

    @property
    def music(self) -> MusicTools | None:
        return self._music if self._settings.computer_control else None

    def on_settings_now(self, old: Settings, new: Settings) -> None:
        """At once, from the dashboard: turning computer control off can't wait."""
        if not new.computer_control:
            self.guard.enabled = False
            self.confirmations.expire(
                "Computer control was turned off.",
                "was dropped because computer control was turned off",
            )
            if self.ducker is not None:
                self.ducker.set_enabled(False, self._state)

    def on_settings(self, old: Settings, new: Settings) -> None:
        """At the start of a question, on the event loop."""
        self._settings = new
        self.guard.enabled = new.computer_control
        self._switch_ducker()
        if new.computer_control and self._music is None and not self._building:
            self._make_music(new)
        if any(getattr(old, a) != getattr(new, a) for a in _CHAT_SETTINGS):
            self._rebuild()

    def before_question(self) -> None:
        """Music tools made in the background since the last question join now, and so
        do changes to the apps and websites lists."""
        lists_changed = (
            self._app_store is not None and self._app_store.snapshot()[0] != self._apps_version
        )
        if self._rebuild_due or lists_changed:
            self._rebuild_due = False
            self._rebuild()

    def on_state(self, state: State) -> None:
        self._state = state
        if self.ducker is not None:
            self.ducker.on_state(state)

    def close(self) -> None:
        if self.ducker is not None:
            self.ducker.close()

    def _switch_ducker(self) -> None:
        if self.ducker is not None:
            s = self._settings
            self.ducker.set_enabled(s.computer_control and s.music_duck, self._state)

    def _make_music(self, settings: Settings) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # not on the loop (tests): just do it
            self._music_made(*_music_tools(settings, self.guard))
            return
        self._building = True

        def make() -> None:
            try:
                made = _music_tools(settings, self.guard)
            except Exception:
                log.warning("Couldn't set up the music tools", exc_info=True)
                made = (None, None)
            loop.call_soon_threadsafe(self._music_made, *made)

        threading.Thread(target=make, name="music-setup", daemon=True).start()

    def _music_made(self, music: MusicTools | None, ducker: MusicDucker | None) -> None:
        self._building = False
        if music is None:
            return
        self._music, self.ducker = music, ducker
        self._switch_ducker()
        self._rebuild_due = True

    def _launch_tools(self, settings: Settings) -> LaunchTools | None:
        """open_app and open_website from the lists as they are now, only when computer
        control is on and a list isn't empty; open_search_result when computer control
        and web search are on. Otherwise CARL's prompt says why not."""
        if self._app_store is None:
            return None
        version, apps, sites = self._app_store.snapshot()
        self._apps_version = version
        pages = settings.web_search and not web_search_blocked()
        if not settings.computer_control:
            log.info("Apps and websites: off (COMPUTER_CONTROL is off)")
            return None
        if apps or sites:
            log.info("Apps and websites: on (%d apps, %d websites)", len(apps), len(sites))
        else:
            log.info("Apps and websites: off (none chosen on the dashboard's Apps tab)")
        log.info("Opening search results: %s", "on (after a spoken yes)" if pages else "off")
        if not (apps or sites or pages):
            return None
        if not pages:
            return LaunchTools(self.guard, apps, sites)
        return LaunchTools(
            self.guard,
            apps,
            sites,
            results=self.search_results,
            confirmations=self.confirmations,
        )

    def _youtube_tools(self, settings: Settings) -> YouTubeTools | None:
        """youtube_search whenever computer control is on; play_youtube only with a
        YOUTUBE_API_KEY too. Otherwise CARL's prompt says why not."""
        if not settings.computer_control:
            log.info("YouTube: off (COMPUTER_CONTROL is off)")
            return None
        if not settings.youtube_api_key:
            log.info("YouTube: search only (no YOUTUBE_API_KEY)")
            return YouTubeTools(self.guard)
        log.info("YouTube: on (play and search)")
        return YouTubeTools(self.guard, YouTubeClient(settings.youtube_api_key))

    def _media_keys(self, settings: Settings) -> MediaKeys | None:
        """media_control whenever computer control is on."""
        if not settings.computer_control:
            log.info("Media keys: off (COMPUTER_CONTROL is off)")
            return None
        log.info("Media keys: on")
        return MediaKeys(self.guard)

    def _rebuild(self) -> None:
        settings = self._settings
        tools = build_tools(
            settings,
            music=self.music,
            launch=self._launch_tools(settings),
            youtube=self._youtube_tools(settings),
            media=self._media_keys(settings),
        )
        chat = create_chat(
            settings,
            tools,
            personality=self._personality,
            bus=self._bus,
            search_results=self.search_results,
        )
        log.info(
            "Note tools: %s",
            f"on ({settings.obsidian_vault_path})"
            if "take_note" in tools
            else "off (no OBSIDIAN_VAULT_PATH)",
        )
        self.tools, self.chat = tools, chat  # both or neither


def _set_speed(synthesizer: Any, speed: float) -> None:
    if hasattr(synthesizer, "set_speed"):  # Piper; SAPI keeps its own pace
        synthesizer.set_speed(speed)


def _music_tools(
    settings: Settings, guard: ActionGuard
) -> tuple[MusicTools | None, MusicDucker | None]:
    """The music tools and their ducker (switched by MUSIC_DUCK), only when computer
    control is on and Spotify is set up and logged in. Otherwise CARL's prompt says music
    isn't set up. Blocking (reads the login from disk): call it off the event loop."""
    if not settings.computer_control:
        reason = "COMPUTER_CONTROL is off"
    elif not settings.spotify_client_id:
        reason = "no SPOTIFY_CLIENT_ID"
    elif (control := create_control(settings.spotify_client_id)) is None:
        reason = "not logged in: run uv run python -m desk_buddy.spotify_login"
    else:
        # Its own client with a short timeout and no retries: a duck is only worth doing
        # at once.
        duck_control = create_control(settings.spotify_client_id, timeout=DUCK_TIMEOUT_S, retries=0)
        ducker = (
            MusicDucker(duck_control, enabled=settings.music_duck)
            if duck_control is not None
            else None
        )
        log.info("Music tools: on (Spotify%s)", ", ducking" if settings.music_duck else "")
        return MusicTools(control, guard, ducker=ducker), ducker
    log.info("Music tools: off (%s)", reason)
    return None, None


async def _load_whisper(settings: Settings) -> WhisperTranscriber:
    return await asyncio.to_thread(
        WhisperTranscriber,
        settings.stt_model,
        device=settings.stt_device,
        compute_type=settings.stt_compute_type,
    )


def _whisper_error(settings: Settings, exc: Exception) -> StartupError:
    return StartupError(
        f"Can't load Whisper model {settings.stt_model!r} on {settings.stt_device} "
        f"({settings.stt_compute_type}): {exc}"
    )


def _settings_without(keys: tuple[str, ...]) -> Settings | None:
    """The settings as if the dashboard hadn't set `keys`, for a second try when one of
    its values (checked, but still unusable, e.g. a model that won't load) breaks startup.
    None if the dashboard sets none of them, so there's nothing to fall back from."""
    values, _ = read_dashboard_values(dashboard_settings_path())
    if not any(key in values for key in keys):
        return None
    try:
        return load_settings(dashboard={k: v for k, v in values.items() if k not in keys})
    except ConfigError:
        return None


async def _make_synthesizer(
    settings: Settings,
) -> tuple[SapiSynthesizer | PiperSynthesizer, Settings]:
    """The TTS engine from settings, and the settings it was made with (a voice from the
    dashboard that won't load falls back to .env's). Piper may download its voice first
    (blocking, in a worker thread; Ctrl+C waits for it, like the Whisper download)."""
    if settings.tts_engine == "sapi":
        try:
            synthesizer = SapiSynthesizer()
        except Exception as exc:
            raise StartupError(f"Can't start Windows SAPI voice: {exc}") from exc
        log.info("Voice: sapi")
        return synthesizer, settings
    try:
        synthesizer = await _load_voice(settings)
    except VoiceError as exc:
        fallback = _settings_without(("TTS_VOICE",))
        if fallback is None or fallback.tts_voice == settings.tts_voice:
            raise StartupError(f"{exc}; or set TTS_ENGINE=sapi to use the Windows voice") from exc
        log.warning(
            "The dashboard's voice didn't load (%s); using %s this time", exc, fallback.tts_voice
        )
        settings = dataclasses.replace(settings, tts_voice=fallback.tts_voice)
        try:
            synthesizer = await _load_voice(settings)
        except VoiceError as exc2:
            raise StartupError(f"{exc2}; or set TTS_ENGINE=sapi to use the Windows voice") from exc2
    log.info("Voice: piper %s", settings.tts_voice)
    return synthesizer, settings


async def _load_voice(settings: Settings) -> PiperSynthesizer:
    return await asyncio.to_thread(
        load_piper, settings.tts_voice, settings.voices_dir, speed=settings.tts_speed
    )


async def _say_farewell(orchestrator: Orchestrator) -> None:
    """Bounded twice: farewell() cuts itself off after FAREWELL_TIMEOUT, and this gives up
    at FAREWELL_HARD_CAP in any case. Any failure is logged; shutdown carries on."""
    try:
        await asyncio.wait_for(orchestrator.farewell(timeout=FAREWELL_TIMEOUT), FAREWELL_HARD_CAP)
    except Exception:
        log.warning("Couldn't say farewell", exc_info=True)


def lan_ip() -> str | None:
    """This PC's address on the LAN: the interface a packet to the internet would leave by.
    Connecting a UDP socket sends nothing; 192.0.2.1 is TEST-NET-1."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 80))
            return sock.getsockname()[0]
    except OSError:
        return None


def key_handler(
    switch: DeviceSwitch, events: asyncio.Queue[str], delay: float = RELEASE_DELAY
) -> Callable[[str], None]:
    """The push-to-talk key's handler (runs on the event loop). While a robot is connected
    the key drives it: down begins the robot's mic, and up ends it `delay` seconds later so
    audio still in flight over Wi-Fi isn't lost.

    A re-press while a release is still pending fires that release at once (end, up),
    then begins again. The robot mic's begin() clears its buffer before the orchestrator
    takes the first clip on that "up", so a double press within `delay` deliberately drops
    the first clip: the orchestrator sees it as an ignored tap, and the second press is
    the turn."""
    pending: list[tuple[asyncio.TimerHandle, Robot]] = []  # at most one delayed release

    def release(robot: Robot) -> None:
        pending.clear()
        if switch.robot is robot:
            robot.mic.end()
        events.put_nowait("up")  # always, so the orchestrator never stays listening

    def on_key(event: str) -> None:
        if pending:  # fire a delayed release now, so the order stays down/up/down
            handle, old = pending[0]
            handle.cancel()
            release(old)
        robot = switch.robot
        if robot is None:
            events.put_nowait(event)
        elif event == "down":
            robot.mic.begin()
            events.put_nowait("down")
        else:
            handle = asyncio.get_running_loop().call_later(delay, release, robot)
            pending.append((handle, robot))

    return on_key


if __name__ == "__main__":
    sys.exit(main())
