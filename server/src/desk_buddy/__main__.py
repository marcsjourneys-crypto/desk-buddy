"""Run the voice loop on the PC: uv run python -m desk_buddy"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import socket
import sys
import threading
from collections.abc import Callable
from typing import Any

from desk_buddy.audio import AudioDeviceError, Microphone, Speaker, check_audio_devices
from desk_buddy.brain.memory import Conversation
from desk_buddy.brain.personality import Personality
from desk_buddy.config import ConfigError, Settings, load_settings
from desk_buddy.dashboard import build_status, create_dashboard, start_dashboard
from desk_buddy.events import EventBus
from desk_buddy.link.switch import DeviceSwitch, Robot
from desk_buddy.llm import create_chat, prompt_for
from desk_buddy.loop import Orchestrator, State
from desk_buddy.personality_store import PersonalityStore
from desk_buddy.ptt import KeyWatcher, parse_key
from desk_buddy.server import ServerStartError, create_app, start_server
from desk_buddy.settings_store import LiveSettings, SettingsStore
from desk_buddy.stt import WhisperTranscriber
from desk_buddy.tools import build_tools
from desk_buddy.tools.actions import ActionGuard
from desk_buddy.tools.ducking import DUCK_TIMEOUT_S, MusicDucker
from desk_buddy.tools.spotify import MusicTools, create_control
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
            transcriber = await asyncio.to_thread(
                WhisperTranscriber,
                settings.stt_model,
                device=settings.stt_device,
                compute_type=settings.stt_compute_type,
            )
        except Exception as exc:
            raise StartupError(
                f"Can't load Whisper model {settings.stt_model!r} on {settings.stt_device} "
                f"({settings.stt_compute_type}): {exc}"
            ) from exc
        synthesizer = await _make_synthesizer(settings)
        stack.callback(synthesizer.close)
        mic = Microphone()
        stack.callback(mic.close)
        speaker = Speaker()
        stack.callback(speaker.close)
        bus = EventBus()  # live events for the dashboard; never blocks the voice loop
        personality = PersonalityStore()  # edited on the dashboard; defaults otherwise
        brain = Brain(settings, bus=bus, personality=personality.current)
        stack.callback(brain.close)  # runs after the farewell, which restores the music
        conversation = Conversation(settings.history_turns)
        # A connected robot takes over the mic, speaker and button; else the PC's are used.
        switch = DeviceSwitch(mic, speaker)
        live = LiveSettings(settings)

        def on_question() -> None:
            live.apply()  # dashboard changes land between turns, never during one
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
        )
        stack.push_async_callback(orchestrator.shutdown)
        live.on_question(brain.on_settings)
        live.on_question(lambda old, new: setattr(orchestrator, "chat", brain.chat))
        live.on_question(lambda old, new: conversation.set_max_turns(new.history_turns))
        live.on_now(lambda old, new: _set_speed(synthesizer, new.tts_speed))

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
            ),
            preview=lambda: prompt_for(live.current, brain.tools, personality.current()),
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
    "music_duck",
    "max_sentences",
    "llm_model",
    "llm_effort",
    "obsidian_vault_path",
    "obsidian_daily_folder",
    "obsidian_ideas_folder",
)
_MUSIC_SETTINGS = ("computer_control", "music_duck")


class Brain:
    """What a turn uses that a live setting can change: the action guard, the music tools
    and their ducker, the tool registry and the chat. Rebuilt between turns (from
    LiveSettings.apply at the start of a question), never during one."""

    def __init__(
        self,
        settings: Settings,
        *,
        bus: EventBus,
        personality: Callable[[], Personality] = Personality,
    ) -> None:
        self.guard = ActionGuard(settings.computer_control, bus=bus)
        self._personality = personality
        self.music: MusicTools | None = None
        self.ducker: MusicDucker | None = None
        self._build(settings, music=True)

    def on_settings(self, old: Settings, new: Settings) -> None:
        self.guard.enabled = new.computer_control
        music = any(getattr(old, a) != getattr(new, a) for a in _MUSIC_SETTINGS)
        if music or any(getattr(old, a) != getattr(new, a) for a in _CHAT_SETTINGS):
            self._build(new, music=music)

    def on_state(self, state: State) -> None:
        if self.ducker is not None:
            self.ducker.on_state(state)

    def close(self) -> None:
        if self.ducker is not None:
            self.ducker.close()

    def _build(self, settings: Settings, *, music: bool) -> None:
        if music:
            old = self.ducker
            self.music, self.ducker = _music_tools(settings, self.guard)
            if old is not None:
                # It restores the music, which can take a moment: never on the event loop.
                threading.Thread(target=old.close, name="duck-close", daemon=True).start()
        self.tools = build_tools(settings, music=self.music)
        log.info(
            "Note tools: %s",
            f"on ({settings.obsidian_vault_path})"
            if "take_note" in self.tools
            else "off (no OBSIDIAN_VAULT_PATH)",
        )
        self.chat = create_chat(settings, self.tools, personality=self._personality)


def _set_speed(synthesizer: Any, speed: float) -> None:
    if hasattr(synthesizer, "set_speed"):  # Piper; SAPI keeps its own pace
        synthesizer.set_speed(speed)


def _music_tools(
    settings: Settings, guard: ActionGuard
) -> tuple[MusicTools | None, MusicDucker | None]:
    """The music tools (and the ducker, with MUSIC_DUCK), only when computer control is on
    and Spotify is set up and logged in. Otherwise CARL's prompt says music isn't set up."""
    if not settings.computer_control:
        reason = "COMPUTER_CONTROL is off"
    elif not settings.spotify_client_id:
        reason = "no SPOTIFY_CLIENT_ID"
    elif (control := create_control(settings.spotify_client_id)) is None:
        reason = "not logged in: run uv run python -m desk_buddy.spotify_login"
    else:
        ducker = None
        if settings.music_duck:
            # Its own client with a short timeout and no retries: a duck is only worth
            # doing at once.
            duck_control = create_control(
                settings.spotify_client_id, timeout=DUCK_TIMEOUT_S, retries=0
            )
            ducker = MusicDucker(duck_control) if duck_control is not None else None
        log.info("Music tools: on (Spotify%s)", ", ducking" if ducker else "")
        return MusicTools(control, guard, ducker=ducker), ducker
    log.info("Music tools: off (%s)", reason)
    return None, None


async def _make_synthesizer(settings: Settings) -> SapiSynthesizer | PiperSynthesizer:
    """The TTS engine from settings. Piper may download its voice first (blocking, in a
    worker thread; Ctrl+C waits for it, like the Whisper download)."""
    if settings.tts_engine == "sapi":
        try:
            synthesizer = SapiSynthesizer()
        except Exception as exc:
            raise StartupError(f"Can't start Windows SAPI voice: {exc}") from exc
        log.info("Voice: sapi")
        return synthesizer
    try:
        synthesizer = await asyncio.to_thread(
            load_piper, settings.tts_voice, settings.voices_dir, speed=settings.tts_speed
        )
    except VoiceError as exc:
        raise StartupError(f"{exc}; or set TTS_ENGINE=sapi to use the Windows voice") from exc
    log.info("Voice: piper %s", settings.tts_voice)
    return synthesizer


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
