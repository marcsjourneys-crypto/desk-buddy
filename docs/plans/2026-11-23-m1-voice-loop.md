# M1 – Voice Loop Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Hold a global key, speak into the PC mic, release, and hear Buddy's streamed spoken reply from the PC speaker.

**Architecture:** One asyncio process. Each blocking piece sits behind a small interface and runs in a thread: mic, Whisper, SAPI, playback. An `Orchestrator` state machine (`idle → listening → thinking → speaking`) runs each turn as one `asyncio.Task`. Claude's text stream is split into sentences, and each sentence is synthesized and played through a 2-slot queue, so speech starts before the reply is complete. Pressing the key mid-turn cancels the task and stops playback. Design: `docs/plans/2026-11-23-m1-voice-loop-design.md`.

**Tech Stack:** Python 3.12, uv, pytest, ruff · `sounddevice` + `numpy` (audio I/O) · `faster-whisper` (STT + its bundled Silero VAD ONNX) · `anthropic` (AsyncAnthropic streaming, `claude-haiku-5-5`) · `comtypes` (Windows SAPI) · ctypes `GetAsyncKeyState` (push-to-talk).

**Conventions for every task**
- Run commands from `server/` (e.g. `cd server` first).
- Before each commit: `uv run pytest` and `uv run ruff check` (and `uv run ruff format`) must be clean.
- Every commit message ends with a blank line and then `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- No pytest-asyncio: async tests wrap their body in `asyncio.run(...)`. That's stdlib, so no new dependency.

---

### Task 1: Dependencies and the `integration` test marker

**Files:**
- Modify: `server/pyproject.toml`
- Modify: `server/uv.lock` (generated)

**Step 1: Add runtime dependencies** (approved in the design)

Run: `uv add sounddevice numpy faster-whisper anthropic comtypes`
Expected: the `dependencies = [...]` list in pyproject.toml contains all five, and uv.lock is updated.

**Step 2: Check that they import**

Run: `uv run python -c "import sounddevice, numpy, faster_whisper, anthropic, comtypes; print('ok')"`
Expected: `ok`

**Step 3: Add the integration marker, skipped by default**

Replace the `[tool.pytest.ini_options]` table in `server/pyproject.toml` with:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-ra -m 'not integration'"
markers = [
    "integration: needs real audio/COM, models or network; run with `uv run pytest -m integration`",
]
```

(Running `-m integration` on the command line overrides the `-m` in addopts, because the last `-m` wins.)

**Step 4: Run the existing tests**

Run: `uv run pytest`
Expected: 3 passed.

**Step 5: Commit**

```bash
git add server/pyproject.toml server/uv.lock
git commit -m "Add M1 runtime dependencies and integration test marker"
```

---

### Task 2: Settings (`config.py`)

**Files:**
- Create: `server/src/desk_buddy/config.py`
- Create: `server/tests/test_config.py`
- Modify: `server/.env.example`, `server/tests/test_smoke.py`

**Step 1: Write the failing tests**

`server/tests/test_config.py`:

```python
from pathlib import Path

import pytest

from desk_buddy.config import ENV_KEYS, ConfigError, Settings, load_settings, parse_env_file

ENV_EXAMPLE = Path(__file__).resolve().parents[1] / ".env.example"


def test_parse_env_file_handles_comments_blanks_and_quotes():
    text = '# comment\n\nA=1\nB = two \nC="quoted value"\nD=\nnot a pair\n'
    assert parse_env_file(text) == {"A": "1", "B": "two", "C": "quoted value", "D": ""}


def test_defaults_apply_when_only_api_key_is_set():
    s = load_settings(env_path=None, environ={"ANTHROPIC_API_KEY": "k"})
    assert s == Settings(anthropic_api_key="k")
    assert s.llm_provider == "anthropic"
    assert s.llm_model == "claude-haiku-5-5"
    assert s.llm_effort == "low"
    assert s.stt_model == "base.en"
    assert s.stt_device == "cpu"
    assert s.stt_compute_type == "int8"
    assert s.tts_engine == "sapi"
    assert s.max_sentences == 2
    assert s.ptt_key == "right_ctrl"


def test_env_file_values_are_used(tmp_path):
    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=file-key\nSTT_MODEL=small.en\nMAX_SENTENCES=3\n")
    s = load_settings(env_path=env, environ={})
    assert (s.anthropic_api_key, s.stt_model, s.max_sentences) == ("file-key", "small.en", 3)


def test_blank_values_fall_back_to_defaults(tmp_path):
    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=k\nSTT_MODEL=\nLLM_MODEL=   \n")
    s = load_settings(env_path=env, environ={})
    assert s.stt_model == "base.en"
    assert s.llm_model == "claude-haiku-5-5"


def test_process_environment_overrides_env_file(tmp_path):
    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=file-key\nSTT_MODEL=small.en\n")
    s = load_settings(env_path=env, environ={"STT_MODEL": "tiny.en"})
    assert s.stt_model == "tiny.en"
    assert s.anthropic_api_key == "file-key"


def test_missing_env_file_is_fine(tmp_path):
    s = load_settings(env_path=tmp_path / "nope.env", environ={"ANTHROPIC_API_KEY": "k"})
    assert s.anthropic_api_key == "k"


def test_missing_api_key_is_a_clear_error():
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        load_settings(env_path=None, environ={})


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("LLM_PROVIDER", "kimi"),
        ("MAX_SENTENCES", "zero"),
        ("MAX_SENTENCES", "0"),
        ("LLM_EFFORT", "turbo"),
        ("STT_DEVICE", "gpu"),
        ("TTS_ENGINE", "piper"),
        ("PTT_KEY", "banana"),
    ],
)
def test_invalid_values_are_rejected(key, value):
    with pytest.raises(ConfigError, match=key):
        load_settings(env_path=None, environ={"ANTHROPIC_API_KEY": "k", key: value})


def test_api_key_is_hidden_from_repr():
    s = load_settings(env_path=None, environ={"ANTHROPIC_API_KEY": "sk-secret"})
    assert "sk-secret" not in repr(s)


def test_every_settings_key_is_in_env_example():
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    listed = set(parse_env_file(text))
    assert set(ENV_KEYS) <= listed
```

**Step 2: Run them and confirm they fail**

Run: `uv run pytest tests/test_config.py -v`
Expected: collection error, `ModuleNotFoundError: No module named 'desk_buddy.config'`.

**Step 3: Implement**

`server/src/desk_buddy/config.py`:

```python
"""Settings loaded from server/.env, overridden by non-blank process environment variables."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from desk_buddy.ptt import parse_key

DEFAULT_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"

# Keys this module reads. Other keys in .env.example belong to later milestones.
ENV_KEYS = (
    "ANTHROPIC_API_KEY",
    "LLM_PROVIDER",
    "LLM_MODEL",
    "LLM_EFFORT",
    "STT_MODEL",
    "STT_DEVICE",
    "STT_COMPUTE_TYPE",
    "TTS_ENGINE",
    "MAX_SENTENCES",
    "PTT_KEY",
)

# The provider seam: each value maps to one Chat class in llm.create_chat().
# OpenAI-compatible providers (Kimi, OpenRouter, Ollama) are planned after M3.
LLM_PROVIDERS = ("anthropic",)
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
STT_DEVICES = ("cpu", "cuda")
TTS_ENGINES = ("sapi",)  # "piper" arrives later behind the same interface


class ConfigError(Exception):
    """A setting is missing or invalid. The message names the key."""


@dataclass(frozen=True)
class Settings:
    anthropic_api_key: str = field(repr=False)
    llm_provider: str = "anthropic"
    llm_model: str = "claude-haiku-5-5"
    llm_effort: str = "low"
    stt_model: str = "base.en"
    stt_device: str = "cpu"
    stt_compute_type: str = "int8"
    tts_engine: str = "sapi"
    max_sentences: int = 2
    ptt_key: str = "right_ctrl"


def parse_env_file(text: str) -> dict[str, str]:
    """Parse KEY=VALUE lines. Ignores blanks, comments and malformed lines; strips quotes."""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def load_settings(
    env_path: Path | None = DEFAULT_ENV_PATH,
    environ: Mapping[str, str] | None = None,
) -> Settings:
    merged: dict[str, str] = {}
    if env_path is not None and env_path.is_file():
        merged.update(parse_env_file(env_path.read_text(encoding="utf-8")))
    env = os.environ if environ is None else environ
    merged.update({k: env[k] for k in ENV_KEYS if env.get(k, "").strip()})

    def get(key: str, default: str) -> str:
        return merged.get(key, "").strip() or default

    api_key = get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise ConfigError(
            "ANTHROPIC_API_KEY is not set. Copy server/.env.example to server/.env and fill it in."
        )

    raw_sentences = get("MAX_SENTENCES", str(Settings.max_sentences))
    try:
        max_sentences = int(raw_sentences)
    except ValueError:
        max_sentences = 0
    if max_sentences < 1:
        raise ConfigError(f"MAX_SENTENCES must be a whole number >= 1, got {raw_sentences!r}")

    provider = _choice(get, "LLM_PROVIDER", Settings.llm_provider, LLM_PROVIDERS)
    effort = _choice(get, "LLM_EFFORT", Settings.llm_effort, EFFORT_LEVELS)
    device = _choice(get, "STT_DEVICE", Settings.stt_device, STT_DEVICES)
    engine = _choice(get, "TTS_ENGINE", Settings.tts_engine, TTS_ENGINES)

    ptt_key = get("PTT_KEY", Settings.ptt_key)
    try:
        parse_key(ptt_key)
    except ValueError as exc:
        raise ConfigError(f"PTT_KEY: {exc}") from exc

    return Settings(
        anthropic_api_key=api_key,
        llm_provider=provider,
        llm_model=get("LLM_MODEL", Settings.llm_model),
        llm_effort=effort,
        stt_model=get("STT_MODEL", Settings.stt_model),
        stt_device=device,
        stt_compute_type=get("STT_COMPUTE_TYPE", Settings.stt_compute_type),
        tts_engine=engine,
        max_sentences=max_sentences,
        ptt_key=ptt_key,
    )


def _choice(get, key: str, default: str, allowed: tuple[str, ...]) -> str:
    value = get(key, default).lower()
    if value not in allowed:
        raise ConfigError(f"{key} must be one of {', '.join(allowed)}; got {value!r}")
    return value
```

This imports `parse_key` from `desk_buddy.ptt`, which Task 3 builds. To keep Task 2 self-contained, create the minimal `ptt.py` now and fill in the rest in Task 3:

`server/src/desk_buddy/ptt.py`:

```python
"""Push-to-talk: a global hold-to-talk key read with Win32 GetAsyncKeyState."""

from __future__ import annotations

# Virtual-key codes: https://learn.microsoft.com/windows/win32/inputdev/virtual-key-codes
VK_CODES: dict[str, int] = {
    "right_ctrl": 0xA3,
    "left_ctrl": 0xA2,
    "right_alt": 0xA5,
    "right_shift": 0xA1,
    "scroll_lock": 0x91,
    "pause": 0x13,
    **{f"f{n}": 0x6F + n for n in range(1, 13)},
}


def parse_key(name: str) -> int:
    """Map a key name (e.g. 'right_ctrl', 'f8') or a hex code (e.g. '0xA3') to a virtual-key code."""
    key = name.strip().lower()
    if key in VK_CODES:
        return VK_CODES[key]
    if key.startswith("0x"):
        try:
            code = int(key, 16)
        except ValueError:
            pass
        else:
            if 0 < code < 0x100:
                return code
    names = ", ".join(sorted(VK_CODES))
    raise ValueError(f"unknown key {name!r}; use one of {names}, or a hex code like 0xA3")
```

**Step 4: Add the new keys to `.env.example`**

In `server/.env.example`, under `# --- LLM ---` before `ANTHROPIC_API_KEY=`, add:

```
# Model provider. Only "anthropic" for now; OpenAI-compatible providers (e.g. Kimi) come later.
LLM_PROVIDER=
```

After `LLM_MODEL=`, add:

```
# Thinking effort: low | medium | high | xhigh | max. Default: low (fastest replies)
LLM_EFFORT=
```

After the `MAX_SENTENCES=` line, add a new section:

```

# --- Push-to-talk (M1, before the robot exists) ---
# Hold this key to talk: right_ctrl, left_ctrl, right_alt, right_shift, scroll_lock, pause,
# f1..f12, or a hex virtual-key code like 0xA3. Default: right_ctrl
PTT_KEY=
```

In `server/tests/test_smoke.py`, add `"LLM_PROVIDER"`, `"LLM_EFFORT"` and `"PTT_KEY"` to `REQUIRED_KEYS`.

**Step 5: Run the tests**

Run: `uv run pytest -v`
Expected: all pass (the 3 smoke tests plus the config tests).

**Step 6: Commit**

```bash
git add server/src/desk_buddy/config.py server/src/desk_buddy/ptt.py server/tests/test_config.py server/tests/test_smoke.py server/.env.example
git commit -m "Add settings loader with .env parsing and validation"
```

---

### Task 3: Push-to-talk watcher (`ptt.py`)

**Files:**
- Modify: `server/src/desk_buddy/ptt.py`
- Create: `server/tests/test_ptt.py`

**Step 1: Write the failing tests**

`server/tests/test_ptt.py`:

```python
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
```

**Step 2: Run them and confirm they fail**

Run: `uv run pytest tests/test_ptt.py -v`
Expected: `ImportError: cannot import name 'EdgeDetector'`.

**Step 3: Implement.** Append to `server/src/desk_buddy/ptt.py`. Add `import ctypes`, `import threading` and `from collections.abc import Callable` to the top imports.

```python
def is_key_pressed(vk: int) -> bool:
    """True while the key is physically held, whichever window has focus."""
    return bool(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000)


class EdgeDetector:
    """Turns a stream of pressed/not-pressed readings into 'down'/'up' events."""

    def __init__(self) -> None:
        self._pressed = False

    def update(self, pressed: bool) -> str | None:
        if pressed == self._pressed:
            return None
        self._pressed = pressed
        return "down" if pressed else "up"


class KeyWatcher:
    """Polls one key on a daemon thread and calls on_event('down' | 'up') on changes.

    on_event runs on the watcher thread; use loop.call_soon_threadsafe to reach asyncio.
    """

    def __init__(
        self,
        vk: int,
        on_event: Callable[[str], None],
        *,
        read_key: Callable[[int], bool] = is_key_pressed,
        interval: float = 0.02,
    ) -> None:
        self._vk = vk
        self._on_event = on_event
        self._read_key = read_key
        self._interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="ptt", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1)

    def _run(self) -> None:
        detector = EdgeDetector()
        while not self._stop.wait(self._interval):
            event = detector.update(self._read_key(self._vk))
            if event:
                self._on_event(event)
```

**Step 4: Run the tests**

Run: `uv run pytest tests/test_ptt.py -v`
Expected: all pass.

**Step 5: Commit**

```bash
git add server/src/desk_buddy/ptt.py server/tests/test_ptt.py
git commit -m "Add global push-to-talk key watcher"
```

---

### Task 4: Sentence splitter

**Files:**
- Create: `server/src/desk_buddy/llm.py` (splitter only for now)
- Create: `server/tests/test_sentences.py`

**Step 1: Write the failing tests**

`server/tests/test_sentences.py`:

```python
from desk_buddy.llm import SentenceSplitter


def split_all(chunks: list[str]) -> list[str]:
    splitter = SentenceSplitter()
    out: list[str] = []
    for chunk in chunks:
        out += splitter.feed(chunk)
    return out + splitter.flush()


def test_simple_sentences():
    assert split_all(["Hello there. How are you?"]) == ["Hello there.", "How are you?"]


def test_sentence_is_released_only_once_confirmed_by_following_space():
    splitter = SentenceSplitter()
    assert splitter.feed("Hello there.") == []  # could still be "there.com"
    assert splitter.feed(" Next") == ["Hello there."]
    assert splitter.flush() == ["Next"]


def test_chunk_boundaries_mid_word():
    assert split_all(["Hello the", "re. How", " are you?"]) == ["Hello there.", "How are you?"]


def test_decimals_do_not_split():
    assert split_all(["It costs 3.5 dollars. Cheap!"]) == ["It costs 3.5 dollars.", "Cheap!"]


def test_abbreviations_do_not_split():
    text = "Dr. Smith said e.g. apples are fine. Yes."
    assert split_all([text]) == ["Dr. Smith said e.g. apples are fine.", "Yes."]


def test_mixed_punctuation_and_ellipsis():
    assert split_all(["Really?! Wow... Okay."]) == ["Really?!", "Wow...", "Okay."]


def test_closing_quote_stays_with_sentence():
    assert split_all(['He said "hi." Then left.']) == ['He said "hi."', "Then left."]


def test_flush_on_empty_returns_nothing():
    assert SentenceSplitter().flush() == []
```

**Step 2: Run them and confirm they fail**

Run: `uv run pytest tests/test_sentences.py -v`
Expected: `ModuleNotFoundError: No module named 'desk_buddy.llm'`.

**Step 3: Implement**

`server/src/desk_buddy/llm.py`:

```python
"""Claude streaming and sentence splitting for spoken replies."""

from __future__ import annotations

import re

_SENTENCE_END = re.compile(r"[.!?]+[\"')\]]*")
_ABBREVIATIONS = {"mr", "mrs", "ms", "dr", "st", "vs", "etc", "e.g", "i.e", "jr", "sr", "prof"}


class SentenceSplitter:
    """Buffers streamed text and releases complete sentences.

    A sentence ends at . ! or ? (plus any closing quotes or brackets) followed by whitespace.
    Text at the end of the buffer is held until more arrives or flush() is called.
    """

    def __init__(self) -> None:
        self._buf = ""

    def feed(self, text: str) -> list[str]:
        self._buf += text
        sentences: list[str] = []
        while (cut := self._find_boundary()) is not None:
            sentence, self._buf = self._buf[:cut].strip(), self._buf[cut:]
            if sentence:
                sentences.append(sentence)
        return sentences

    def flush(self) -> list[str]:
        rest, self._buf = self._buf.strip(), ""
        return [rest] if rest else []

    def _find_boundary(self) -> int | None:
        for match in _SENTENCE_END.finditer(self._buf):
            end = match.end()
            if end >= len(self._buf):
                return None  # wait for the next chunk to confirm
            if not self._buf[end].isspace():
                continue  # "3.5", "e.g" mid-token, "example.com"
            if match.group() == "." and self._last_word(match.start()) in _ABBREVIATIONS:
                continue
            return end
        return None

    def _last_word(self, pos: int) -> str:
        words = self._buf[:pos].split()
        return words[-1].lower().lstrip("\"'([") if words else ""
```

**Step 4: Run the tests**

Run: `uv run pytest tests/test_sentences.py -v`
Expected: all pass.

**Step 5: Commit**

```bash
git add server/src/desk_buddy/llm.py server/tests/test_sentences.py
git commit -m "Add sentence splitter for streamed replies"
```

---

### Task 5: Placeholder personality and the Claude streaming client

**Files:**
- Create: `server/src/desk_buddy/brain/personality.py`
- Modify: `server/src/desk_buddy/llm.py`
- Create: `server/tests/test_llm.py`

**Step 1: Write the failing tests**

`server/tests/test_llm.py`:

```python
import asyncio
from types import SimpleNamespace

import anthropic
import httpx
import pytest

from desk_buddy.brain.personality import system_prompt
from desk_buddy.llm import ClaudeChat, LLMError, LLMRefusal


class FakeStream:
    def __init__(self, deltas, stop_reason="end_turn", error=None):
        self._deltas, self._stop_reason, self._error = deltas, stop_reason, error

    async def __aenter__(self):
        if self._error:
            raise self._error
        return self

    async def __aexit__(self, *exc):
        return False

    @property
    def text_stream(self):
        async def gen():
            for d in self._deltas:
                yield d

        return gen()

    async def get_final_message(self):
        return SimpleNamespace(stop_reason=self._stop_reason)


class FakeClient:
    def __init__(self, stream: FakeStream):
        self.calls: list[dict] = []
        self.messages = SimpleNamespace(stream=self._stream)
        self._s = stream

    def _stream(self, **kwargs):
        self.calls.append(kwargs)
        return self._s


def collect(chat: ClaudeChat, text: str) -> list[str]:
    async def run():
        return [d async for d in chat.stream_reply(text)]

    return asyncio.run(run())


def make_chat(stream: FakeStream) -> tuple[ClaudeChat, FakeClient]:
    client = FakeClient(stream)
    return ClaudeChat(client, model="m", effort="low", system="sys"), client


def test_streams_text_and_sends_settings():
    chat, client = make_chat(FakeStream(["Hel", "lo."]))
    assert collect(chat, "hi") == ["Hel", "lo."]
    call = client.calls[0]
    assert call["model"] == "m"
    assert call["system"] == "sys"
    assert call["output_config"] == {"effort": "low"}
    assert call["messages"] == [{"role": "user", "content": "hi"}]


def test_refusal_raises_llm_refusal():
    chat, _ = make_chat(FakeStream([], stop_reason="refusal"))
    with pytest.raises(LLMRefusal):
        collect(chat, "hi")


def test_connection_error_becomes_llm_error():
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    chat, _ = make_chat(FakeStream([], error=anthropic.APIConnectionError(request=request)))
    with pytest.raises(LLMError, match="reach"):
        collect(chat, "hi")


def test_create_chat_builds_claude_for_anthropic_provider():
    from desk_buddy.config import Settings
    from desk_buddy.llm import create_chat

    assert isinstance(create_chat(Settings(anthropic_api_key="k")), ClaudeChat)


def test_system_prompt_mentions_limits():
    prompt = system_prompt(max_sentences=3)
    assert "3" in prompt
    assert "markdown" in prompt.lower()
```

**Step 2: Run them and confirm they fail**

Run: `uv run pytest tests/test_llm.py -v`
Expected: ImportError (`personality` / `ClaudeChat` missing).

**Step 3: Implement**

`server/src/desk_buddy/brain/personality.py` (an M1 placeholder; M2 replaces it with the real character, emotion tags and canned lines):

```python
"""Buddy's system prompt. M1 placeholder: short, spoken, no markdown."""


def system_prompt(max_sentences: int) -> str:
    return (
        "You are Buddy, a friendly little desk robot. Everything you write is spoken aloud, "
        "so reply in plain conversational sentences: no markdown, no lists, no emoji, no code. "
        f"Keep replies to at most {max_sentences} short sentences unless asked for more."
    )
```

Append to `server/src/desk_buddy/llm.py`. Add `import logging`, `from collections.abc import AsyncIterator`, `from typing import Any` and `import anthropic` to the imports in ruff order, plus `log = logging.getLogger(__name__)`.

```python
MAX_TOKENS = 2048  # thinking counts toward this; spoken replies are short


class LLMError(Exception):
    """Claude could not produce a reply (network, API error). Already retried by the SDK."""


class LLMRefusal(LLMError):
    """Claude declined the request (stop_reason == 'refusal')."""


class ClaudeChat:
    """One-turn streaming chat. Session memory arrives in M2."""

    def __init__(self, client: Any, *, model: str, effort: str, system: str) -> None:
        # client: anthropic.AsyncAnthropic (typed Any so tests can pass a fake)
        self._client = client
        self._model = model
        self._effort = effort
        self._system = system

    async def stream_reply(self, user_text: str) -> AsyncIterator[str]:
        """Yield text deltas as they arrive. Raises LLMError / LLMRefusal."""
        try:
            async with self._client.messages.stream(
                model=self._model,
                max_tokens=MAX_TOKENS,
                system=self._system,
                output_config={"effort": self._effort},
                messages=[{"role": "user", "content": user_text}],
            ) as stream:
                async for text in stream.text_stream:
                    yield text
                message = await stream.get_final_message()
        except anthropic.RateLimitError as exc:
            raise LLMError("rate limited by the API") from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(f"API error {exc.status_code}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError("can't reach the API") from exc
        if message.stop_reason == "refusal":
            raise LLMRefusal("Claude declined to answer")


def create_chat(settings: Settings) -> ClaudeChat:
    """Build the chat client for settings.llm_provider. Add new providers here."""
    from desk_buddy.brain.personality import system_prompt

    if settings.llm_provider == "anthropic":
        return ClaudeChat(
            anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key),
            model=settings.llm_model,
            effort=settings.llm_effort,
            system=system_prompt(settings.max_sentences),
        )
    raise ValueError(f"unsupported LLM_PROVIDER {settings.llm_provider!r}")
```

Also add `from desk_buddy.config import Settings` to the imports. Every provider class implements the same `stream_reply(user_text) -> AsyncIterator[str]` and raises `LLMError` / `LLMRefusal`, so the voice loop never imports a provider SDK.

Notes for the implementer:
- `claude-haiku-5-5` runs adaptive thinking by default. Thinking blocks never show up in `text_stream`. `effort: "low"` keeps latency down.
- Don't set `temperature` or a `thinking` budget: Haiku 5.5 returns a 400 for both.
- Haiku 5.5 has no server-side refusal fallback, so a refusal is just reported.

**Step 4: Run the tests**

Run: `uv run pytest tests/test_llm.py -v`
Expected: all pass.

**Step 5: Commit**

```bash
git add server/src/desk_buddy/brain/personality.py server/src/desk_buddy/llm.py server/tests/test_llm.py
git commit -m "Add placeholder system prompt and streaming Claude client"
```

---

### Task 6: Audio I/O (`audio.py`)

**Files:**
- Create: `server/src/desk_buddy/audio.py`
- Create: `server/tests/test_audio.py`

**Step 1: Write the failing tests** (pure buffer logic only; real devices are covered by the manual check)

`server/tests/test_audio.py`:

```python
from desk_buddy.audio import BYTES_PER_SECOND, PcmBuffer, duration_seconds


def test_duration():
    assert duration_seconds(b"\x00" * BYTES_PER_SECOND) == 1.0


def test_buffer_collects_and_resets():
    buf = PcmBuffer(max_seconds=1)
    buf.append(b"ab")
    buf.append(b"cd")
    assert buf.take() == b"abcd"
    assert buf.take() == b""


def test_buffer_caps_size():
    buf = PcmBuffer(max_seconds=0.001)  # 32 bytes at 16 kHz int16
    buf.append(b"x" * 20)
    buf.append(b"y" * 20)
    buf.append(b"z" * 20)
    assert buf.take() == b"x" * 20 + b"y" * 12
```

**Step 2: Run them and confirm they fail**

Run: `uv run pytest tests/test_audio.py -v`
Expected: `ModuleNotFoundError`.

**Step 3: Implement**

`server/src/desk_buddy/audio.py`:

```python
"""PC mic and speaker via sounddevice. Audio everywhere is 16 kHz, 16-bit, mono PCM bytes."""

from __future__ import annotations

import threading

import sounddevice as sd

SAMPLE_RATE = 16_000
BYTES_PER_SECOND = SAMPLE_RATE * 2
CHUNK_BYTES = BYTES_PER_SECOND // 50  # 20 ms


class AudioDeviceError(Exception):
    """No usable input or output device."""


def duration_seconds(pcm: bytes) -> float:
    return len(pcm) / BYTES_PER_SECOND


def check_audio_devices() -> None:
    """Fail fast with the device list if there is no default mic or speaker."""
    try:
        sd.query_devices(kind="input")
        sd.query_devices(kind="output")
    except (ValueError, sd.PortAudioError) as exc:
        raise AudioDeviceError(f"{exc}\nAvailable devices:\n{sd.query_devices()}") from exc


class PcmBuffer:
    """Thread-safe PCM accumulator with a size cap (extra audio is dropped)."""

    def __init__(self, max_seconds: float) -> None:
        self._max = int(max_seconds * BYTES_PER_SECOND)
        self._chunks: list[bytes] = []
        self._size = 0
        self._lock = threading.Lock()

    def append(self, data: bytes) -> None:
        with self._lock:
            room = self._max - self._size
            if room <= 0:
                return
            chunk = data[:room]
            self._chunks.append(chunk)
            self._size += len(chunk)

    def take(self) -> bytes:
        with self._lock:
            out = b"".join(self._chunks)
            self._chunks, self._size = [], 0
            return out


class Microphone:
    """Keeps the input stream open (no start-up delay); only buffers between start() and stop()."""

    def __init__(self, max_seconds: float = 30.0) -> None:
        self._buffer = PcmBuffer(max_seconds)
        self._recording = threading.Event()
        self._stream = sd.RawInputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="int16",
            blocksize=CHUNK_BYTES // 2,
            callback=self._callback,
        )
        self._stream.start()

    def _callback(self, indata, frames, time_info, status) -> None:
        if self._recording.is_set():
            self._buffer.append(bytes(indata))

    def start(self) -> None:
        self._buffer.take()
        self._recording.set()

    def stop(self) -> bytes:
        self._recording.clear()
        return self._buffer.take()

    def close(self) -> None:
        self._stream.stop()
        self._stream.close()


class Speaker:
    """Plays PCM through one long-lived output stream, so sentences play back to back with no gap."""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._stream = sd.RawOutputStream(
            samplerate=SAMPLE_RATE, channels=1, dtype="int16", latency="low"
        )
        self._stream.start()

    def play(self, pcm: bytes) -> None:
        """Blocking; returns early (within ~20 ms) when stop() is called."""
        self._stop.clear()
        pcm = pcm[: len(pcm) // 2 * 2]
        for i in range(0, len(pcm), CHUNK_BYTES):
            if self._stop.is_set():
                return
            self._stream.write(pcm[i : i + CHUNK_BYTES])

    def stop(self) -> None:
        self._stop.set()

    def close(self) -> None:
        self._stop.set()
        self._stream.stop()
        self._stream.close()
```

`play()` clears the stop flag when it starts. A stale `play()` from a cancelled turn exits on its next 20 ms chunk, which is long before the next turn's first `play()` (STT alone takes hundreds of ms). That's why a plain Event is enough here.

**Step 4: Run the tests**

Run: `uv run pytest tests/test_audio.py -v`
Expected: all pass.

**Step 5: Commit**

```bash
git add server/src/desk_buddy/audio.py server/tests/test_audio.py
git commit -m "Add PC microphone and speaker on sounddevice"
```

---

### Task 7: SAPI text-to-speech (`tts.py`)

**Files:**
- Create: `server/src/desk_buddy/tts.py`
- Create: `server/tests/integration/__init__.py` (empty)
- Create: `server/tests/integration/test_sapi.py`

**Step 1: Write the failing integration test**

`server/tests/integration/test_sapi.py`:

```python
import pytest

from desk_buddy.audio import BYTES_PER_SECOND, duration_seconds
from desk_buddy.tts import SapiSynthesizer

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def synth():
    s = SapiSynthesizer()
    yield s
    s.close()


def test_output_format_is_16k_16bit_mono(synth):
    assert synth.audio_format() == (16_000, 16, 1)


def test_synthesizes_speech(synth):
    pcm = synth.synthesize("Hello, I am Buddy.")
    assert len(pcm) % 2 == 0
    assert 0.3 < duration_seconds(pcm) < 5
    assert len(pcm) > BYTES_PER_SECOND // 4


def test_markup_is_spoken_as_text_not_xml(synth):
    # With SVSFIsNotXML, '<' is read as text instead of being parsed as SAPI XML.
    assert len(synth.synthesize("<volume level='0'>quiet</volume>")) > 0
```

**Step 2: Run it and confirm it fails**

Run: `uv run pytest -m integration tests/integration/test_sapi.py -v`
Expected: `ModuleNotFoundError: No module named 'desk_buddy.tts'`.

**Step 3: Implement**

`server/src/desk_buddy/tts.py`:

```python
"""Text-to-speech. Every engine returns 16 kHz 16-bit mono PCM bytes."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Protocol

SAFT_16KHZ_16BIT_MONO = 18  # SpeechAudioFormatType
SVSF_IS_NOT_XML = 16  # SpeechVoiceSpeakFlags: never parse model text as SAPI XML


class Synthesizer(Protocol):
    def synthesize(self, text: str) -> bytes: ...


class SapiSynthesizer:
    """Windows SAPI voice via comtypes.

    COM objects belong to the thread that made them, so every SAPI call runs on one
    dedicated worker thread. synthesize() is blocking and safe to call from any thread.
    """

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="sapi", initializer=_co_initialize
        )
        self._executor.submit(self._create).result()

    def synthesize(self, text: str) -> bytes:
        return self._executor.submit(self._synthesize, text).result()

    def audio_format(self) -> tuple[int, int, int]:
        """(samples/s, bits/sample, channels) of the output; used by tests."""
        return self._executor.submit(self._audio_format).result()

    def close(self) -> None:
        self._executor.shutdown(wait=False)

    # --- runs on the SAPI thread ---

    def _create(self) -> None:
        import comtypes.client

        self._voice = comtypes.client.CreateObject("SAPI.SpVoice")

    def _new_stream(self):
        import comtypes.client

        fmt = comtypes.client.CreateObject("SAPI.SpAudioFormat")
        fmt.Type = SAFT_16KHZ_16BIT_MONO
        stream = comtypes.client.CreateObject("SAPI.SpMemoryStream")
        stream.Format = fmt
        return stream

    def _synthesize(self, text: str) -> bytes:
        stream = self._new_stream()
        self._voice.AudioOutputStream = stream
        self._voice.Speak(text, SVSF_IS_NOT_XML)
        return bytes(stream.GetData())

    def _audio_format(self) -> tuple[int, int, int]:
        wfx = self._new_stream().Format.GetWaveFormatEx()
        return (wfx.SamplesPerSec, wfx.BitsPerSample, wfx.Channels)


def _co_initialize() -> None:
    import comtypes

    comtypes.CoInitialize()
```

If `stream.Format = fmt` fails or `test_output_format_is_16k_16bit_mono` fails, use `stream.Format.Type = SAFT_16KHZ_16BIT_MONO` instead (the VBScript idiom). Let the test decide; don't guess further. The first run generates comtypes typelib wrappers, which takes a few seconds.

**Step 4: Run the tests**

Run: `uv run pytest -m integration tests/integration/test_sapi.py -v`
Expected: 3 passed.
Run: `uv run pytest`
Expected: unit tests pass and the integration tests show as deselected.

**Step 5: Commit**

```bash
git add server/src/desk_buddy/tts.py server/tests/integration/
git commit -m "Add Windows SAPI synthesizer via comtypes"
```

---

### Task 8: Speech-to-text with VAD gate (`stt.py`)

**Files:**
- Create: `server/src/desk_buddy/stt.py`
- Create: `server/tests/test_stt.py`, `server/tests/integration/test_stt.py`

**Step 1: Write the failing tests**

`server/tests/test_stt.py`:

```python
import numpy as np

from desk_buddy.stt import pcm_to_float32


def test_pcm_to_float32_scales_to_unit_range():
    pcm = np.array([0, 16384, -32768], dtype=np.int16).tobytes()
    assert pcm_to_float32(pcm).tolist() == [0.0, 0.5, -1.0]
```

`server/tests/integration/test_stt.py`:

```python
import pytest

from desk_buddy.audio import BYTES_PER_SECOND
from desk_buddy.stt import WhisperTranscriber
from desk_buddy.tts import SapiSynthesizer

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def transcriber():
    return WhisperTranscriber("base.en", device="cpu", compute_type="int8")


def test_silence_is_no_speech(transcriber):
    assert transcriber.transcribe(b"\x00" * BYTES_PER_SECOND * 2) == ""


def test_tts_to_stt_round_trip(transcriber):
    synth = SapiSynthesizer()
    try:
        pcm = synth.synthesize("Hello buddy, what time is it?")
    finally:
        synth.close()
    text = transcriber.transcribe(pcm).lower()
    assert "hello" in text
    assert "time" in text
```

**Step 2: Run them and confirm they fail**

Run: `uv run pytest tests/test_stt.py -v`
Expected: `ModuleNotFoundError`.

**Step 3: Implement**

`server/src/desk_buddy/stt.py`:

```python
"""Speech-to-text: Silero VAD gate + trim, then faster-whisper."""

from __future__ import annotations

from typing import Protocol

import numpy as np


class Transcriber(Protocol):
    def transcribe(self, pcm: bytes) -> str:
        """Return the spoken text, or '' if the clip has no speech."""
        ...


def pcm_to_float32(pcm: bytes) -> np.ndarray:
    return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0


class WhisperTranscriber:
    """faster-whisper with vad_filter=True: the Silero ONNX model it bundles drops
    non-speech, so taps and silence come back as '' (no 'Thank you.' hallucinations)."""

    def __init__(self, model_size: str, *, device: str, compute_type: str) -> None:
        from faster_whisper import WhisperModel

        # First use downloads the model (~150 MB for base.en) into the Hugging Face cache.
        self._model = WhisperModel(model_size, device=device, compute_type=compute_type)

    def transcribe(self, pcm: bytes) -> str:
        segments, _info = self._model.transcribe(
            pcm_to_float32(pcm),
            language="en",
            vad_filter=True,
            beam_size=1,  # greedy: fastest on CPU; revisit if accuracy is poor
            condition_on_previous_text=False,
        )
        return " ".join(seg.text.strip() for seg in segments).strip()
```

**Step 4: Run the tests**

Run: `uv run pytest tests/test_stt.py -v`
Expected: pass.
Run: `uv run pytest -m integration tests/integration/test_stt.py -v`
Expected: 2 passed. The first run downloads the model.

**Step 5: Commit**

```bash
git add server/src/desk_buddy/stt.py server/tests/test_stt.py server/tests/integration/test_stt.py
git commit -m "Add faster-whisper transcriber with VAD gate"
```

---

### Task 9: Orchestrator (`loop.py`)

**Files:**
- Create: `server/src/desk_buddy/loop.py`
- Create: `server/tests/test_loop.py`

**Step 1: Write the failing tests**

`server/tests/test_loop.py`:

```python
import asyncio
import threading

from desk_buddy.audio import BYTES_PER_SECOND
from desk_buddy.llm import LLMError, LLMRefusal
from desk_buddy.loop import CANNED_ERROR, CANNED_REFUSAL, Orchestrator, State

ONE_SECOND = b"\x00" * BYTES_PER_SECOND


class FakeMic:
    def __init__(self, pcm: bytes = ONE_SECOND):
        self.pcm, self.starts = pcm, 0

    def start(self):
        self.starts += 1

    def stop(self) -> bytes:
        return self.pcm


class FakeTranscriber:
    def __init__(self, text: str = "hi buddy"):
        self.text, self.calls = text, 0

    def transcribe(self, pcm: bytes) -> str:
        self.calls += 1
        return self.text


class FakeChat:
    def __init__(self, deltas=("Hello there. ", "How are you?"), error: Exception | None = None):
        self.deltas, self.error, self.prompts = deltas, error, []

    async def stream_reply(self, text: str):
        self.prompts.append(text)
        for d in self.deltas:
            yield d
        if self.error:
            raise self.error


class FakeSynth:
    def synthesize(self, text: str) -> bytes:
        return text.encode()


class FakeSpeaker:
    def __init__(self, block: bool = False):
        self.played: list[str] = []
        self.stops = 0
        self.block = block
        self.started = threading.Event()
        self._release = threading.Event()

    def play(self, pcm: bytes) -> None:
        self.played.append(pcm.decode())
        self.started.set()
        if self.block:
            self._release.wait(timeout=5)

    def stop(self) -> None:
        self.stops += 1
        self._release.set()


def make(**overrides):
    parts = {
        "mic": FakeMic(),
        "transcriber": FakeTranscriber(),
        "chat": FakeChat(),
        "synthesizer": FakeSynth(),
        "speaker": FakeSpeaker(),
    }
    parts.update(overrides)
    states: list[State] = []
    orch = Orchestrator(**parts, on_state=states.append)
    return orch, parts, states


async def talk(orch: Orchestrator) -> None:
    await orch.handle("down")
    await orch.handle("up")
    await orch.wait_idle()


def test_normal_turn():
    orch, parts, states = make()
    asyncio.run(talk(orch))
    assert states == [State.LISTENING, State.THINKING, State.SPEAKING, State.IDLE]
    assert parts["chat"].prompts == ["hi buddy"]
    assert parts["speaker"].played == ["Hello there.", "How are you?"]


def test_short_tap_is_ignored():
    orch, parts, states = make(mic=FakeMic(pcm=b"\x00" * 100))
    asyncio.run(talk(orch))
    assert states == [State.LISTENING, State.IDLE]
    assert parts["transcriber"].calls == 0


def test_no_speech_goes_idle_without_calling_claude():
    orch, parts, states = make(transcriber=FakeTranscriber(text=""))
    asyncio.run(talk(orch))
    assert states == [State.LISTENING, State.THINKING, State.IDLE]
    assert parts["chat"].prompts == []


def test_api_error_speaks_canned_line():
    orch, parts, states = make(chat=FakeChat(deltas=(), error=LLMError("boom")))
    asyncio.run(talk(orch))
    assert parts["speaker"].played == [CANNED_ERROR]
    assert states[-1] is State.IDLE


def test_error_after_partial_reply_keeps_spoken_part():
    orch, parts, _ = make(chat=FakeChat(deltas=("First. ",), error=LLMError("boom")))
    asyncio.run(talk(orch))
    assert parts["speaker"].played == ["First.", CANNED_ERROR]


def test_refusal_speaks_refusal_line():
    orch, parts, _ = make(chat=FakeChat(deltas=(), error=LLMRefusal("no")))
    asyncio.run(talk(orch))
    assert parts["speaker"].played == [CANNED_REFUSAL]


def test_up_without_down_is_ignored():
    orch, parts, states = make()
    asyncio.run(orch.handle("up"))
    assert states == []
    assert parts["transcriber"].calls == 0


def test_key_down_while_speaking_interrupts_and_listens():
    speaker = FakeSpeaker(block=True)
    orch, parts, states = make(speaker=speaker)

    async def scenario():
        await orch.handle("down")
        await orch.handle("up")
        assert await asyncio.to_thread(speaker.started.wait, 2)
        await orch.handle("down")  # interrupt
        assert orch.state is State.LISTENING

    asyncio.run(scenario())
    assert speaker.stops == 1
    assert speaker.played == ["Hello there."]  # second sentence never played
    assert parts["mic"].starts == 2
    assert states[-1] is State.LISTENING
```

**Step 2: Run them and confirm they fail**

Run: `uv run pytest tests/test_loop.py -v`
Expected: `ModuleNotFoundError: No module named 'desk_buddy.loop'`.

**Step 3: Implement**

`server/src/desk_buddy/loop.py`:

```python
"""The voice loop: push-to-talk events in, spoken replies out."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Callable
from enum import StrEnum
from typing import Protocol

from desk_buddy.audio import duration_seconds
from desk_buddy.llm import LLMError, LLMRefusal, SentenceSplitter
from desk_buddy.stt import Transcriber
from desk_buddy.tts import Synthesizer

log = logging.getLogger(__name__)

MIN_CLIP_SECONDS = 0.3
CANNED_ERROR = "Sorry, I can't reach my brain right now."
CANNED_REFUSAL = "Hmm, I'd better not answer that one."


class State(StrEnum):
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"


class Mic(Protocol):
    def start(self) -> None: ...
    def stop(self) -> bytes: ...


class Player(Protocol):
    def play(self, pcm: bytes) -> None: ...
    def stop(self) -> None: ...


class Chat(Protocol):
    def stream_reply(self, user_text: str) -> AsyncIterator[str]: ...


class Orchestrator:
    """State machine. One asyncio.Task per turn; a key press mid-turn cancels it."""

    def __init__(
        self,
        *,
        mic: Mic,
        transcriber: Transcriber,
        chat: Chat,
        synthesizer: Synthesizer,
        speaker: Player,
        on_state: Callable[[State], None] = lambda state: None,
    ) -> None:
        self.mic = mic
        self.transcriber = transcriber
        self.chat = chat
        self.synthesizer = synthesizer
        self.speaker = speaker
        self.state = State.IDLE
        self._on_state = on_state
        self._turn: asyncio.Task[None] | None = None
        self._t0 = 0.0

    # --- events ---

    async def run(self, events: asyncio.Queue[str]) -> None:
        while True:
            await self.handle(await events.get())

    async def handle(self, event: str) -> None:
        if event == "down":
            await self._interrupt()
            self.mic.start()
            self._set_state(State.LISTENING)
        elif event == "up" and self.state is State.LISTENING:
            pcm = self.mic.stop()
            seconds = duration_seconds(pcm)
            if seconds < MIN_CLIP_SECONDS:
                log.info("Ignored %.2f s tap", seconds)
                self._set_state(State.IDLE)
                return
            self._set_state(State.THINKING)
            self._t0 = time.perf_counter()
            self._turn = asyncio.create_task(self._run_turn(pcm))

    async def wait_idle(self) -> None:
        if self._turn:
            await self._turn

    async def shutdown(self) -> None:
        await self._interrupt()

    # --- internals ---

    def _set_state(self, state: State) -> None:
        if state is not self.state:
            self.state = state
            log.debug("state -> %s", state)
            self._on_state(state)

    def _ms(self) -> float:
        return (time.perf_counter() - self._t0) * 1000

    async def _interrupt(self) -> None:
        turn, self._turn = self._turn, None
        if turn and not turn.done():
            log.info("Interrupted")
            turn.cancel()
            self.speaker.stop()
            with contextlib.suppress(asyncio.CancelledError):
                await turn

    async def _run_turn(self, pcm: bytes) -> None:
        try:
            text = await asyncio.to_thread(self.transcriber.transcribe, pcm)
            log.info("STT %.0f ms", self._ms())
            if not text:
                log.info("No speech detected")
                return
            log.info("You: %s", text)
            queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=2)
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self._produce(text, queue))
                tg.create_task(self._consume(queue))
        except Exception:
            log.exception("Turn failed")
        finally:
            if not asyncio.current_task().cancelling():
                self._set_state(State.IDLE)

    async def _produce(self, text: str, queue: asyncio.Queue[bytes | None]) -> None:
        """Claude deltas -> sentences -> PCM, into the queue. None marks the end."""
        splitter = SentenceSplitter()
        reply: list[str] = []
        first_token = True
        try:
            async with contextlib.aclosing(self.chat.stream_reply(text)) as deltas:
                async for delta in deltas:
                    if first_token:
                        log.info("First token %.0f ms", self._ms())
                        first_token = False
                    for sentence in splitter.feed(delta):
                        reply.append(sentence)
                        await self._enqueue(sentence, queue)
            for sentence in splitter.flush():
                reply.append(sentence)
                await self._enqueue(sentence, queue)
        except LLMRefusal:
            log.warning("Claude refused")
            await self._enqueue(CANNED_REFUSAL, queue)
        except LLMError as exc:
            log.warning("LLM error: %s", exc)
            await self._enqueue(CANNED_ERROR, queue)
        log.info("Buddy: %s", " ".join(reply))
        await queue.put(None)

    async def _enqueue(self, sentence: str, queue: asyncio.Queue[bytes | None]) -> None:
        await queue.put(await asyncio.to_thread(self.synthesizer.synthesize, sentence))

    async def _consume(self, queue: asyncio.Queue[bytes | None]) -> None:
        while (pcm := await queue.get()) is not None:
            if self.state is not State.SPEAKING:
                log.info("First audio %.0f ms", self._ms())
                self._set_state(State.SPEAKING)
            await asyncio.to_thread(self.speaker.play, pcm)
```

Why `cancelling()` in `finally`: a turn that was cancelled by an interrupt must not set `idle`, because the orchestrator is already `listening` for the new turn.

**Step 4: Run the tests**

Run: `uv run pytest tests/test_loop.py -v`
Expected: all pass. If `test_key_down_while_speaking_interrupts_and_listens` hangs, check that `_interrupt` calls `speaker.stop()`; that call is what releases the blocked `play` thread.

**Step 5: Commit**

```bash
git add server/src/desk_buddy/loop.py server/tests/test_loop.py
git commit -m "Add voice loop orchestrator with interrupts"
```

---

### Task 10: Entry point (`__main__.py`) and the live Claude test

**Files:**
- Create: `server/src/desk_buddy/__main__.py`
- Create: `server/tests/integration/test_claude_live.py`

**Step 1: Write the live integration test**

`server/tests/integration/test_claude_live.py`:

```python
import asyncio

import anthropic
import pytest

from desk_buddy.brain.personality import system_prompt
from desk_buddy.config import ConfigError, load_settings
from desk_buddy.llm import ClaudeChat

pytestmark = pytest.mark.integration


def test_live_streaming_reply():
    try:
        settings = load_settings()
    except ConfigError:
        pytest.skip("ANTHROPIC_API_KEY not configured")
    chat = ClaudeChat(
        anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key),
        model=settings.llm_model,
        effort=settings.llm_effort,
        system=system_prompt(settings.max_sentences),
    )

    async def run():
        return "".join([d async for d in chat.stream_reply("Say just the word pineapple.")])

    assert "pineapple" in asyncio.run(run()).lower()
```

**Step 2: Implement the entry point**

`server/src/desk_buddy/__main__.py`:

```python
"""Run the voice loop on the PC: uv run python -m desk_buddy"""

from __future__ import annotations

import asyncio
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
    except (ConfigError, AudioDeviceError) as exc:
        print(f"Can't start: {exc}", file=sys.stderr)
        return 2
    try:
        asyncio.run(_run(settings))
    except KeyboardInterrupt:
        pass
    log.info("Bye")
    return 0


async def _run(settings: Settings) -> None:
    log.info("Loading speech-to-text model %s (first run downloads it)...", settings.stt_model)
    transcriber = await asyncio.to_thread(
        WhisperTranscriber,
        settings.stt_model,
        device=settings.stt_device,
        compute_type=settings.stt_compute_type,
    )
    synthesizer = SapiSynthesizer()
    mic = Microphone()
    speaker = Speaker()
    chat = create_chat(settings)
    orchestrator = Orchestrator(
        mic=mic, transcriber=transcriber, chat=chat, synthesizer=synthesizer, speaker=speaker
    )

    loop = asyncio.get_running_loop()
    events: asyncio.Queue[str] = asyncio.Queue()
    watcher = KeyWatcher(
        parse_key(settings.ptt_key),
        lambda event: loop.call_soon_threadsafe(events.put_nowait, event),
    )
    watcher.start()
    log.info("Ready. Hold %s to talk, Ctrl+C to quit.", settings.ptt_key)
    try:
        await orchestrator.run(events)
    finally:
        watcher.stop()
        await orchestrator.shutdown()
        mic.close()
        speaker.close()
        synthesizer.close()


if __name__ == "__main__":
    sys.exit(main())
```

**Step 3: Run the integration tests** (needs `server/.env` with a real key)

Run: `uv run pytest -m integration -v`
Expected: all integration tests pass (SAPI, STT, live Claude).

**Step 4: Run the whole suite and lint**

Run: `uv run pytest` then `uv run ruff check` then `uv run ruff format --check`
Expected: all clean.

**Step 5: Commit**

```bash
git add server/src/desk_buddy/__main__.py server/tests/integration/test_claude_live.py
git commit -m "Add entry point wiring the PC voice loop"
```

---

### Task 11: Manual check, docs, and close M1

**Files:**
- Modify: `docs/decisions.md`, `CLAUDE.md`, `server/README.md`

**Step 1: Manual checklist.** Run `uv run python -m desk_buddy` from `server/`, then:
- [ ] Startup logs "Ready. Hold right_ctrl to talk".
- [ ] Hold Right Ctrl and ask "What's the capital of France?". The reply is spoken, and the log shows STT, first token and first audio times.
- [ ] Ask for something longer ("Tell me three fun facts about owls"). The first sentence starts before the reply has finished streaming.
- [ ] Press Right Ctrl mid-reply. Speech stops within about 0.1 s and the log shows "Interrupted" and then the new turn.
- [ ] A quick tap is logged as "Ignored … tap".
- [ ] Holding the key in silence for 2 s logs "No speech detected".
- [ ] It works while another window (e.g. a browser) has focus.
- [ ] Ctrl+C exits cleanly with "Bye".

Write down the typical latencies (STT ms / first token ms / first audio ms) for the decisions entry.

**Step 2: Update `docs/decisions.md`** with a new dated section and these entries:
- **Audio I/O: sounddevice confirmed.** Mic input stream kept open (buffers only while the key is held); one long-lived output stream for gapless sentences. · Alternatives: PyAudio.
- **SAPI via comtypes on a dedicated COM thread**, rendering to an in-memory 16 kHz stream with `SVSFIsNotXML`. · Alternatives: PowerShell subprocess, pywin32, pyttsx3.
- **VAD: faster-whisper's bundled Silero ONNX (`vad_filter=True`).** No PyTorch. · Alternatives: silero-vad package.
- **Push-to-talk: global hold key via `GetAsyncKeyState` (ctypes), `PTT_KEY` default Right Ctrl.** · Alternatives: Enter toggle, tap + VAD end.
- **Interrupts: key down mid-turn cancels the turn task and stops playback.**
- **LLM effort `low` by default (`LLM_EFFORT`)** for latency on Haiku 5.5; measured latencies: … · Alternatives: default `medium`.
- **Whisper `beam_size=1`** (greedy) for CPU latency.
- **LLM provider seam: `LLM_PROVIDER` (only `anthropic` for now) + `llm.create_chat()`.** The loop depends only on `stream_reply()`; OpenAI-compatible providers (Kimi, OpenRouter, Ollama) are planned after M3, once tool calling is settled. · Alternatives: Kimi support in M1; no seam.
- **Async tests use `asyncio.run`**, so no pytest-asyncio.

**Step 3: Update `CLAUDE.md`** (Server stack section):
- Audio I/O line: `sounddevice (PortAudio) for the PC mic/speaker.` (drop "confirm in M1").
- VAD line: `Silero VAD — the ONNX model bundled with faster-whisper (vad_filter), no PyTorch.`
- TTS line: replace "(zero setup; pick the library in M1)" with "(via comtypes)".
- LLM line: add "Provider is pluggable via `LLM_PROVIDER` (only `anthropic` today); OpenAI-compatible providers such as Kimi are planned after M3."
- Add under "How we work": `Integration tests (real audio/COM/models/network) are marked and skipped by default: uv run pytest -m integration.`

**Step 4: Update `server/README.md`** with:
- Run: `uv run python -m desk_buddy` (hold Right Ctrl to talk; change it with `PTT_KEY`).
- Integration tests: `uv run pytest -m integration`.

**Step 5: Final verification and commit**

Run: `uv run pytest` and `uv run ruff check`
Expected: all clean.

```bash
git add docs/decisions.md CLAUDE.md server/README.md
git commit -m "Record M1 decisions and document running the voice loop"
```
