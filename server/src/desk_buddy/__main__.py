"""Run the voice loop on the PC: uv run python -m desk_buddy"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys

from desk_buddy.audio import AudioDeviceError, Microphone, Speaker, check_audio_devices
from desk_buddy.config import ConfigError, Settings, load_settings
from desk_buddy.llm import create_chat
from desk_buddy.loop import Orchestrator
from desk_buddy.ptt import KeyWatcher, parse_key
from desk_buddy.stt import WhisperTranscriber
from desk_buddy.tts import SapiSynthesizer

log = logging.getLogger("desk_buddy")


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
    except (ConfigError, AudioDeviceError) as exc:
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
        log.info("Loading speech-to-text model %s (first run downloads it)...", settings.stt_model)
        transcriber = await asyncio.to_thread(
            WhisperTranscriber,
            settings.stt_model,
            device=settings.stt_device,
            compute_type=settings.stt_compute_type,
        )
        synthesizer = SapiSynthesizer()
        stack.callback(synthesizer.close)
        mic = Microphone()
        stack.callback(mic.close)
        speaker = Speaker()
        stack.callback(speaker.close)
        chat = create_chat(settings)
        orchestrator = Orchestrator(
            mic=mic, transcriber=transcriber, chat=chat, synthesizer=synthesizer, speaker=speaker
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
