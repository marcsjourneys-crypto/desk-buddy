"""Run the voice loop on the PC: uv run python -m desk_buddy"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys

from desk_buddy.audio import AudioDeviceError, Microphone, Speaker, check_audio_devices
from desk_buddy.brain.memory import Conversation
from desk_buddy.config import ConfigError, Settings, load_settings
from desk_buddy.llm import create_chat
from desk_buddy.loop import Orchestrator
from desk_buddy.ptt import KeyWatcher, parse_key
from desk_buddy.stt import WhisperTranscriber
from desk_buddy.tools import build_tools
from desk_buddy.tts import SapiSynthesizer

log = logging.getLogger("desk_buddy")


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
        orchestrator = Orchestrator(
            mic=mic,
            transcriber=transcriber,
            chat=chat,
            synthesizer=synthesizer,
            speaker=speaker,
            conversation=Conversation(settings.history_turns),
        )
        stack.push_async_callback(orchestrator.shutdown)

        loop = asyncio.get_running_loop()
        events: asyncio.Queue[str] = asyncio.Queue()
        watcher = KeyWatcher(
            parse_key(settings.ptt_key),
            lambda event: loop.call_soon_threadsafe(events.put_nowait, event),
        )
        watcher.start()
        stack.callback(watcher.stop)
        log.info("Ready. Hold %s to talk, Ctrl+C to quit.", settings.ptt_key)
        await orchestrator.run(events)


if __name__ == "__main__":
    sys.exit(main())
