"""Run the voice loop on the PC: uv run python -m desk_buddy"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from collections.abc import Callable

from desk_buddy.audio import AudioDeviceError, Microphone, Speaker, check_audio_devices
from desk_buddy.brain.memory import Conversation
from desk_buddy.config import ConfigError, Settings, load_settings
from desk_buddy.link.switch import DeviceSwitch, Robot
from desk_buddy.llm import create_chat
from desk_buddy.loop import Orchestrator
from desk_buddy.ptt import KeyWatcher, parse_key
from desk_buddy.server import ServerStartError, create_app, start_server
from desk_buddy.stt import WhisperTranscriber
from desk_buddy.tools import build_tools
from desk_buddy.tts import SapiSynthesizer

log = logging.getLogger("desk_buddy")

RELEASE_DELAY = 0.25  # seconds the robot's mic stays open after the key is released


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
        pass
    log.info("Bye")
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
        try:
            synthesizer = SapiSynthesizer()
        except Exception as exc:
            raise StartupError(f"Can't start Windows SAPI voice: {exc}") from exc
        stack.callback(synthesizer.close)
        mic = Microphone()
        stack.callback(mic.close)
        speaker = Speaker()
        stack.callback(speaker.close)
        tools = build_tools(settings)
        log.info(
            "Note tools: %s",
            f"on ({settings.obsidian_vault_path})" if tools else "off (no OBSIDIAN_VAULT_PATH)",
        )
        chat = create_chat(settings, tools)
        # A connected robot takes over the mic, speaker and button; else the PC's are used.
        switch = DeviceSwitch(mic, speaker)
        orchestrator = Orchestrator(
            mic=switch.mic,
            transcriber=transcriber,
            chat=chat,
            synthesizer=synthesizer,
            speaker=switch.player,
            conversation=Conversation(settings.history_turns),
            on_state=switch.send_state,
            on_emotion=switch.send_emotion,
        )
        stack.push_async_callback(orchestrator.shutdown)

        events: asyncio.Queue[str] = asyncio.Queue()
        if settings.robot_token:
            app = create_app(
                token=settings.robot_token,
                switch=switch,
                events=events,
                current_state=lambda: orchestrator.state.value,
            )
            try:
                server = await start_server(app, settings.server_host, settings.server_port)
            except ServerStartError as exc:
                raise StartupError(str(exc)) from exc
            stack.push_async_callback(server.stop)
            log.info(
                "Robot link: on (ws://%s:%d/robot)", settings.server_host, settings.server_port
            )
        else:
            log.info("Robot link: off (no ROBOT_TOKEN)")

        loop = asyncio.get_running_loop()
        on_key = key_handler(switch, events)
        watcher = KeyWatcher(
            parse_key(settings.ptt_key),
            lambda event: loop.call_soon_threadsafe(on_key, event),
        )
        watcher.start()
        stack.callback(watcher.stop)
        log.info("Ready. Hold %s to talk, Ctrl+C to quit.", settings.ptt_key)
        await orchestrator.run(events)


def key_handler(
    switch: DeviceSwitch, events: asyncio.Queue[str], delay: float = RELEASE_DELAY
) -> Callable[[str], None]:
    """The push-to-talk key's handler (runs on the event loop). While a robot is connected
    the key drives it: down begins the robot's mic, and up ends it `delay` seconds later so
    audio still in flight over Wi-Fi isn't lost."""
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
