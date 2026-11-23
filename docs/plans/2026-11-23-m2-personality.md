# M2 – Personality Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Give Buddy a character, emotion tags, short spoken replies, and memory of the conversation until restart, with a spoken "forget" command.

**Architecture:**
- Two new pure modules in `brain/`:
  - `emotion.py` is a streaming tag parser.
  - `memory.py` holds the `Conversation` and forget-phrase matching.
- `personality.py` gets the real prompt and the canned lines.
- The `Chat` protocol gains a `history` argument.
- The orchestrator owns the `Conversation`. It records each sentence as it starts playing, and in the turn's `finally` it decides what to remember. That covers normal, interrupted, failed and forget turns.

**Tech stack:** Python 3.12 standard library only (re, collections.deque, dataclasses). No new dependencies.

**Design:** `docs/plans/2026-11-23-m2-personality-design.md`

**Conventions (from M1):**
- Run every command from `server/`.
- Tests use plain `asyncio.run`, not pytest-asyncio.
- Run `uv run pytest` and `uv run ruff check` before every commit, and `uv run ruff format` on touched files.
- Never edit `server/.env`.
- End every commit message with the attribution lines given in the session.

---

### Task 1: Emotion tag parser

**Files:**
- Create: `server/src/desk_buddy/brain/emotion.py`
- Test: `server/tests/test_emotion.py`

**Step 1: Write the failing tests**

```python
import pytest

from desk_buddy.brain.emotion import DEFAULT_EMOTION, EMOTIONS, EmotionParser


def run(chunks):
    p = EmotionParser()
    out = "".join(p.feed(c) for c in chunks) + p.flush()
    return p.emotion, out


def test_leading_tag_is_stripped_and_reported():
    assert run(["[happy] Hi there."]) == ("happy", "Hi there.")


def test_tag_split_across_chunks():
    assert run(["[", "hap", "py]", " ", "Hi."]) == ("happy", "Hi.")


def test_leading_whitespace_before_tag():
    assert run(["\n [curious] Ooh."]) == ("curious", "Ooh.")


def test_tag_is_case_insensitive():
    assert run(["[Surprised] Wow."]) == ("surprised", "Wow.")


def test_missing_tag_is_neutral_and_text_passes_through():
    assert run(["Hello there."]) == (DEFAULT_EMOTION, "Hello there.")


def test_unknown_tag_is_neutral_and_kept():
    assert run(["[angry] Grr."]) == (DEFAULT_EMOTION, "[angry] Grr.")


def test_non_tag_brackets_are_kept():
    assert run(["[1] see above."]) == (DEFAULT_EMOTION, "[1] see above.")


def test_known_tags_mid_reply_are_removed():
    assert run(["[happy] Hi! [sad] Bye."]) == ("happy", "Hi!  Bye.")


def test_mid_reply_tag_split_across_chunks_is_removed():
    assert run(["[happy] Hi! [sur", "prised] Oh."]) == ("happy", "Hi!  Oh.")


def test_emotion_is_known_as_soon_as_the_tag_completes():
    p = EmotionParser()
    assert p.feed("[thin") == ""
    assert p.emotion is None
    p.feed("king] Hmm")
    assert p.emotion == "thinking"


def test_empty_reply_is_neutral():
    assert run([]) == (DEFAULT_EMOTION, "")


def test_lone_bracket_at_end_is_released_on_flush():
    assert run(["Hi ["]) == (DEFAULT_EMOTION, "Hi [")


def test_every_emotion_is_recognised():
    for e in EMOTIONS:
        assert run([f"[{e}] x"]) == (e, "x")


TEXT = "  [curious] Labs love water! [happy] They [1] swim. [odd] Really [sad]"


@pytest.mark.parametrize("size", [1, 2, 3, 5, 7, 11])
def test_output_does_not_depend_on_chunking(size):
    chunks = [TEXT[i : i + size] for i in range(0, len(TEXT), size)]
    assert run(chunks) == run([TEXT])
```

**Step 2: Run them and check they fail**

Run: `uv run pytest tests/test_emotion.py -q`
Expected: collection error, `ModuleNotFoundError: desk_buddy.brain.emotion`.

**Step 3: Implement**

```python
"""Emotion tags. Every reply starts with one, e.g. "[happy] Hi!"; it drives the eyes."""

from __future__ import annotations

import re

EMOTIONS = ("neutral", "happy", "sad", "surprised", "sleepy", "thinking", "curious")
DEFAULT_EMOTION = "neutral"

_TAG = re.compile(r"\[(" + "|".join(EMOTIONS) + r")\]", re.IGNORECASE)
_HOLD = max(len(e) for e in EMOTIONS) + 2  # length of the longest tag, "[surprised]"


class EmotionParser:
    """Splits a streamed reply into its emotion and the text to speak.

    The leading tag (after optional whitespace) sets `emotion` and is removed together
    with the whitespace after it. No tag, or an unknown one, means DEFAULT_EMOTION and
    the text passes through. Known tags later in the reply are removed so they are never
    spoken; other bracketed text such as "[1]" is kept. Text that could still turn into
    a tag is held back until it is decided. The output does not depend on chunking.
    """

    def __init__(self) -> None:
        self.emotion: str | None = None  # None until the leading tag is decided
        self._buf = ""
        self._skip_space = False

    def feed(self, text: str) -> str:
        self._buf += text
        if self.emotion is None and not self._decide_lead(final=False):
            return ""
        return self._release(final=False)

    def flush(self) -> str:
        if self.emotion is None:
            self._decide_lead(final=True)
        return self._release(final=True)

    def _decide_lead(self, *, final: bool) -> bool:
        head = self._buf.lstrip()
        if not final:
            if not head:
                return False
            if head.startswith("[") and "]" not in head and len(head) < _HOLD:
                return False  # could still become a tag
        match = _TAG.match(head)
        if match:
            self.emotion = match.group(1).lower()
            self._buf = head[match.end() :]
            self._skip_space = True
        else:
            self.emotion = DEFAULT_EMOTION
            self._buf = head
        return True

    def _release(self, *, final: bool) -> str:
        if self._skip_space:
            self._buf = self._buf.lstrip()
            self._skip_space = not self._buf
        text, hold = self._buf, ""
        if not final:
            i = text.rfind("[")
            if i != -1 and "]" not in text[i:] and len(text) - i < _HOLD:
                text, hold = text[:i], text[i:]
        self._buf = hold
        return _TAG.sub("", text)
```

**Step 4: Run the tests and check they pass**

Run: `uv run pytest tests/test_emotion.py -q`
Expected: all pass.

**Step 5: Commit**

```bash
git add src/desk_buddy/brain/emotion.py tests/test_emotion.py
git commit -m "Add streaming emotion tag parser"
```

---

### Task 2: Conversation memory and forget phrases

**Files:**
- Create: `server/src/desk_buddy/brain/memory.py`
- Test: `server/tests/test_memory.py`

**Step 1: Write the failing tests**

```python
import pytest

from desk_buddy.brain.memory import Conversation, is_forget_command


def test_messages_alternate_user_and_assistant_in_order():
    c = Conversation()
    c.add("hi", "[happy] Hello!")
    c.add("how are you", "[neutral] Fine.")
    assert c.messages() == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "[happy] Hello!"},
        {"role": "user", "content": "how are you"},
        {"role": "assistant", "content": "[neutral] Fine."},
    ]
    assert len(c) == 2


def test_cap_drops_oldest_exchange():
    c = Conversation(max_turns=2)
    for i in range(3):
        c.add(f"q{i}", f"a{i}")
    assert [m["content"] for m in c.messages()] == ["q1", "a1", "q2", "a2"]


def test_forget_empties_memory():
    c = Conversation()
    c.add("hi", "hello")
    c.forget()
    assert c.messages() == []
    assert len(c) == 0


def test_messages_returns_a_copy():
    c = Conversation()
    c.messages().append({"role": "user", "content": "x"})
    assert c.messages() == []


def test_max_turns_must_be_positive():
    with pytest.raises(ValueError):
        Conversation(max_turns=0)


@pytest.mark.parametrize(
    "text",
    [
        "Forget that.",
        "forget it",
        "Forget everything!",
        "New topic.",
        "Start over.",
        "Let's start over.",
        "Clear your memory.",
        "Change the subject.",
        "Buddy, new topic.",
        "Okay, forget that.",
        "Hey Buddy, start over please.",
    ],
)
def test_forget_phrases_match(text):
    assert is_forget_command(text)


@pytest.mark.parametrize(
    "text",
    [
        "Don't forget to call Mum.",
        "Can we start over on the plan for Tuesday?",
        "What's a good new topic for my blog?",
        "Forget",
        "",
        "Buddy",
    ],
)
def test_near_misses_do_not_match(text):
    assert not is_forget_command(text)
```

**Step 2: Run them and check they fail**

Run: `uv run pytest tests/test_memory.py -q`
Expected: `ModuleNotFoundError: desk_buddy.brain.memory`.

**Step 3: Implement**

```python
"""Conversation memory until the server restarts, and the spoken "forget" command."""

from __future__ import annotations

import re
from collections import deque

# Matched after normalising: lowercase, punctuation removed ("let's" -> "lets"),
# leading/trailing fillers dropped. Whisper's spelling varies, so extend here.
FORGET_PHRASES = frozenset(
    {
        "forget that",
        "forget it",
        "forget everything",
        "new topic",
        "start over",
        "lets start over",
        "clear your memory",
        "change the subject",
    }
)
_LEADING_FILLERS = frozenset({"hey", "buddy", "okay", "ok", "please", "so", "um", "uh"})
_TRAILING_FILLERS = frozenset({"buddy", "please"})


class Conversation:
    """The last max_turns exchanges of (user text, Buddy's reply), oldest first."""

    def __init__(self, max_turns: int = 20) -> None:
        if max_turns < 1:
            raise ValueError("max_turns must be >= 1")
        self._turns: deque[tuple[str, str]] = deque(maxlen=max_turns)

    def add(self, user: str, reply: str) -> None:
        self._turns.append((user, reply))

    def forget(self) -> None:
        self._turns.clear()

    def messages(self) -> list[dict[str, str]]:
        """A fresh list in Messages API shape: user, assistant, user, assistant, ..."""
        out: list[dict[str, str]] = []
        for user, reply in self._turns:
            out.append({"role": "user", "content": user})
            out.append({"role": "assistant", "content": reply})
        return out

    def __len__(self) -> int:
        return len(self._turns)


def is_forget_command(text: str) -> bool:
    words = re.sub(r"[^\w\s]", "", text.lower()).split()
    while words and words[0] in _LEADING_FILLERS:
        words.pop(0)
    while words and words[-1] in _TRAILING_FILLERS:
        words.pop()
    return " ".join(words) in FORGET_PHRASES
```

**Note for the implementer:** `Conversation` defines `__len__`, so an empty one is falsy. Always write `if conversation is None`, never `conversation or ...`.

**Step 4: Run the tests and check they pass**

Run: `uv run pytest tests/test_memory.py -q`
Expected: all pass.

**Step 5: Commit**

```bash
git add src/desk_buddy/brain/memory.py tests/test_memory.py
git commit -m "Add conversation memory and forget-phrase matching"
```

---

### Task 3: Personality prompt and canned lines

**Files:**
- Modify: `server/src/desk_buddy/brain/personality.py` (rewrite)
- Test: `server/tests/test_personality.py` (new). Delete `test_system_prompt_mentions_limits` from `server/tests/test_llm.py`; it moves here.

**Step 1: Write the failing tests**

```python
from desk_buddy.brain.emotion import EMOTIONS
from desk_buddy.brain.personality import (
    ERROR_LINE,
    FORGET_LINE,
    REFUSAL_LINE,
    system_prompt,
)


def test_prompt_mentions_sentence_limit_and_spoken_style():
    prompt = system_prompt(max_sentences=3)
    assert "3 short sentences" in prompt
    assert "markdown" in prompt.lower()


def test_prompt_lists_every_emotion_tag():
    prompt = system_prompt(2)
    for e in EMOTIONS:
        assert f"[{e}]" in prompt


def test_prompt_explains_interrupted_marker():
    assert "(interrupted)" in system_prompt(2)


def test_canned_lines_use_known_emotions_and_have_text():
    for line in (ERROR_LINE, REFUSAL_LINE, FORGET_LINE):
        assert line.emotion in EMOTIONS
        assert line.text.strip()
```

**Step 2: Run them and check they fail**

Run: `uv run pytest tests/test_personality.py -q`
Expected: `ImportError` (no `ERROR_LINE`).

**Step 3: Implement** (replace the whole file)

```python
"""Buddy's character: the system prompt and the canned lines spoken without Claude."""

from __future__ import annotations

from typing import NamedTuple

from desk_buddy.brain.emotion import EMOTIONS

INTERRUPTED = "(interrupted)"


class Line(NamedTuple):
    emotion: str
    text: str


ERROR_LINE = Line("sad", "Sorry, I can't reach my brain right now.")
REFUSAL_LINE = Line("neutral", "Hmm, I'd better not answer that one.")
FORGET_LINE = Line("happy", "Okay, fresh start.")


def system_prompt(max_sentences: int) -> str:
    tags = " ".join(f"[{e}]" for e in EMOTIONS)
    return f"""\
You are Buddy, a small retro tin robot who lives on the user's desk. You are warm, \
upbeat and calm: friendly without being hyper or gushing. Now and then, when it fits, \
you slip in a dry, witty remark, but never at the user's expense and never when they \
sound frustrated. You speak in the first person and never call yourself an AI or a \
language model.

Everything you say is spoken aloud by a text-to-speech voice. Use plain conversational \
sentences only: no markdown, lists, headings, emoji, code or links. Write numbers, \
symbols and units the way you would say them.

Keep replies to at most {max_sentences} short sentences, around thirty words in total, \
unless the user asks for more. If you are not sure what the user means, ask a quick \
question.

Start every reply with exactly one emotion tag that fits your tone, chosen from: {tags}. \
Use no other tags. For example: [curious] Ooh, what are you building?

If one of your earlier replies ends with {INTERRUPTED}, the user cut you off there and \
only heard what came before it."""
```

**Step 4: Run the tests and check they pass**

Run: `uv run pytest -q`
Expected: all pass. `loop.py` still has its own canned constants; Task 6 switches it over.

**Step 5: Commit**

```bash
git add src/desk_buddy/brain/personality.py tests/test_personality.py tests/test_llm.py
git commit -m "Write Buddy's personality prompt and canned lines"
```

---

### Task 4: `HISTORY_TURNS` setting

**Files:**
- Modify: `server/src/desk_buddy/config.py`
- Modify: `server/.env.example`
- Test: `server/tests/test_config.py`

**Step 1: Write the failing tests**

In `test_config.py`:
- Add `assert s.history_turns == 20` to `test_defaults_apply_when_only_api_key_is_set`.
- Add `("HISTORY_TURNS", "0")` and `("HISTORY_TURNS", "lots")` to the `test_invalid_values_are_rejected` parameters.
- Add:

```python
def test_history_turns_is_read(tmp_path):
    s = load_settings(env_path=None, environ={"ANTHROPIC_API_KEY": "k", "HISTORY_TURNS": "5"})
    assert s.history_turns == 5
```

**Step 2: Run them and check they fail**

Run: `uv run pytest tests/test_config.py -q`
Expected: failures on `history_turns`.

**Step 3: Implement**

In `config.py`:
- Add `"HISTORY_TURNS",` to `ENV_KEYS`, after `"MAX_SENTENCES"`.
- Add the field `history_turns: int = 20` after `max_sentences`.
- Replace the `MAX_SENTENCES` parsing block with a helper used for both keys:

```python
def _positive_int(get: Callable[[str, str], str], key: str, default: int) -> int:
    raw = get(key, str(default))
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value < 1:
        raise ConfigError(f"{key} must be a whole number >= 1, got {raw!r}")
    return value
```

and in `load_settings`:

```python
    max_sentences = _positive_int(get, "MAX_SENTENCES", Settings.max_sentences)
    history_turns = _positive_int(get, "HISTORY_TURNS", Settings.history_turns)
```

Then pass `history_turns=history_turns` into `Settings(...)`.

In `.env.example`, under `# --- Personality ---`, after `MAX_SENTENCES=`:

```
# How many recent exchanges Buddy remembers (cleared on restart or by saying "new topic"). Default: 20
HISTORY_TURNS=
```

**Step 4: Run the tests and check they pass**

Run: `uv run pytest -q`
Expected: all pass. `test_every_settings_key_is_in_env_example` covers the new key.

**Step 5: Commit**

```bash
git add src/desk_buddy/config.py .env.example tests/test_config.py
git commit -m "Add HISTORY_TURNS setting"
```

---

### Task 5: Chat takes the conversation history

**Files:**
- Modify: `server/src/desk_buddy/llm.py` (`Chat`, `ClaudeChat.stream_reply`)
- Modify: `server/src/desk_buddy/loop.py:150` (call site, temporary `[]`)
- Test: `server/tests/test_llm.py`, `server/tests/test_loop.py` (fakes), `server/tests/integration/test_claude_live.py`

**Step 1: Write the failing test and update callers in the tests**

In `test_llm.py`:
- Change `collect`:

```python
def collect(chat: ClaudeChat, text: str, history=()) -> list[str]:
    async def run():
        return [d async for d in chat.stream_reply(list(history), text)]

    return asyncio.run(run())
```

- Change the two direct calls `chat.stream_reply("hi")` (in `test_error_mid_stream_after_text` and `test_breaking_out_early_closes_stream`) to `chat.stream_reply([], "hi")`.
- Add:

```python
def test_history_is_sent_before_the_new_message():
    chat, client = make_chat(FakeStream(["ok"]))
    history = [
        {"role": "user", "content": "tell me about labs"},
        {"role": "assistant", "content": "[happy] Labs are great."},
    ]
    collect(chat, "are they good hunters?", history)
    assert client.calls[0]["messages"] == [
        *history,
        {"role": "user", "content": "are they good hunters?"},
    ]
    assert len(history) == 2  # caller's list is not modified
```

In `test_loop.py`, change both fakes' signatures:
- `FakeChat.stream_reply(self, history, text)`: also record `self.histories.append(list(history))`, and initialise `self.histories = []` in `__init__`.
- `HoldingChat.stream_reply(self, history, text)`.

In `integration/test_claude_live.py`, change the call to `chat.stream_reply([], "Say just the word pineapple.")`.

**Step 2: Run them and check they fail**

Run: `uv run pytest -q`
Expected: `test_llm.py` fails with a TypeError (wrong argument count), and `test_loop.py` fails too.

**Step 3: Implement**

In `llm.py`:

```python
class Chat(Protocol):
    """What the voice loop needs from any LLM provider."""

    def stream_reply(
        self, history: Sequence[Mapping[str, str]], user_text: str
    ) -> AsyncGenerator[str, None]: ...
```

Add `Mapping, Sequence` to the `collections.abc` import. In `ClaudeChat`:
- Change the docstring to `"""Streaming chat. The caller owns the history (see brain.memory)."""`.
- Change the signature to `async def stream_reply(self, history, user_text)` with the same types.
- Use `messages=[*history, {"role": "user", "content": user_text}],`.

In `loop.py` `_produce`, change the call to `self.chat.stream_reply([], text)`. Task 6 replaces this.

**Step 4: Run the tests and check they pass**

Run: `uv run pytest -q`
Expected: all pass.

**Step 5: Commit**

```bash
git add src/desk_buddy/llm.py src/desk_buddy/loop.py tests/test_llm.py tests/test_loop.py tests/integration/test_claude_live.py
git commit -m "Pass conversation history to the chat provider"
```

---

### Task 6: Loop uses emotions, memory and the forget command

**Files:**
- Modify: `server/src/desk_buddy/loop.py`
- Test: `server/tests/test_loop.py`

**Step 1: Update the test helpers and write the failing tests**

Imports at the top of `test_loop.py`:

```python
from desk_buddy.brain.memory import Conversation
from desk_buddy.brain.personality import ERROR_LINE, FORGET_LINE, REFUSAL_LINE
from desk_buddy.loop import Orchestrator, State
```

Then:
- Replace `CANNED_ERROR` with `ERROR_LINE.text` and `CANNED_REFUSAL` with `REFUSAL_LINE.text` everywhere.
- Change `make()` so that it builds the conversation and collects emotions:

```python
def make(**overrides):
    parts = {
        "mic": FakeMic(),
        "transcriber": FakeTranscriber(),
        "chat": FakeChat(),
        "synthesizer": FakeSynth(),
        "speaker": FakeSpeaker(),
        "conversation": Conversation(),
    }
    parts.update(overrides)
    states: list[State] = []
    emotions: list[str] = []
    orch = Orchestrator(**parts, on_state=states.append, on_emotion=emotions.append)
    parts["emotions"] = emotions
    return orch, parts, states
```

- Update `test_reply_log_includes_canned_line` to expect `f"Buddy [neutral]: First. {ERROR_LINE.text}"`.

New tests:

```python
def test_second_turn_sees_first_turn_in_history():
    orch, parts, _ = make()

    async def scenario():
        await talk(orch)
        await talk(orch)

    asyncio.run(scenario())
    assert parts["chat"].histories[0] == []
    assert parts["chat"].histories[1] == [
        {"role": "user", "content": "hi buddy"},
        {"role": "assistant", "content": "[neutral] Hello there. How are you?"},
    ]


def test_emotion_tag_is_reported_stripped_and_remembered():
    orch, parts, _ = make(chat=FakeChat(deltas=("[hap", "py] Hi", " there.")))
    asyncio.run(talk(orch))
    assert parts["emotions"] == ["happy"]
    assert parts["speaker"].played == ["Hi there."]
    assert parts["conversation"].messages()[1]["content"] == "[happy] Hi there."


def test_interrupt_remembers_only_what_started_playing():
    speaker = FakeSpeaker(block=True)
    orch, parts, _ = make(speaker=speaker)

    async def scenario():
        await orch.handle("down")
        await orch.handle("up")
        assert await asyncio.to_thread(speaker.started.wait, 2)
        await orch.handle("down")  # interrupt during "Hello there."

    asyncio.run(scenario())
    assert parts["conversation"].messages()[1]["content"] == "[neutral] Hello there. (interrupted)"


class SlowChat:
    """Never yields; records that it was called."""

    def __init__(self):
        self.called = asyncio.Event()

    async def stream_reply(self, history, text):
        self.called.set()
        await asyncio.Event().wait()
        yield ""  # pragma: no cover


def test_interrupt_before_any_speech_remembers_nothing():
    async def scenario():
        chat = SlowChat()
        orch, parts, _ = make(chat=chat)
        await orch.handle("down")
        await orch.handle("up")
        await asyncio.wait_for(chat.called.wait(), 2)
        await orch.handle("down")  # interrupt while thinking
        return parts

    parts = asyncio.run(scenario())
    assert len(parts["conversation"]) == 0


def test_failed_turn_is_not_remembered_and_looks_sad():
    orch, parts, _ = make(chat=FakeChat(deltas=("First. ",), error=LLMError("boom")))
    asyncio.run(talk(orch))
    assert len(parts["conversation"]) == 0
    assert parts["emotions"] == ["neutral", "sad"]


def test_refusal_is_not_remembered():
    orch, parts, _ = make(chat=FakeChat(deltas=(), error=LLMRefusal("no")))
    asyncio.run(talk(orch))
    assert len(parts["conversation"]) == 0


def test_forget_phrase_clears_memory_without_calling_claude():
    conversation = Conversation()
    conversation.add("tell me about labs", "[happy] Labs are great.")
    orch, parts, states = make(
        transcriber=FakeTranscriber(text="Buddy, new topic."), conversation=conversation
    )
    asyncio.run(talk(orch))
    assert parts["chat"].prompts == []
    assert len(conversation) == 0
    assert parts["speaker"].played == [FORGET_LINE.text]
    assert parts["emotions"] == [FORGET_LINE.emotion]
    assert states == [State.LISTENING, State.THINKING, State.SPEAKING, State.IDLE]
```

**Step 2: Run them and check they fail**

Run: `uv run pytest tests/test_loop.py -q`
Expected: `TypeError: unexpected keyword argument 'conversation'` (or `on_emotion`).

**Step 3: Implement**

In `loop.py`:
- Delete `CANNED_ERROR` and `CANNED_REFUSAL`.
- Add these imports:

```python
from dataclasses import dataclass, field

from desk_buddy.brain.emotion import DEFAULT_EMOTION, EmotionParser
from desk_buddy.brain.memory import Conversation, is_forget_command
from desk_buddy.brain.personality import (
    ERROR_LINE,
    FORGET_LINE,
    INTERRUPTED,
    REFUSAL_LINE,
    Line,
)
```

Add, above `Orchestrator`:

```python
@dataclass
class _Turn:
    """What happened in one turn, for deciding what to remember."""

    user: str
    emotion: str | None = None
    spoken: list[str] = field(default_factory=list)  # sentences that started playing
    failed: bool = False
    complete: bool = False
```

Constructor: add the keyword arguments `conversation: Conversation | None = None` and `on_emotion: Callable[[str], None] = lambda emotion: None`. Store them with:

```python
        self.conversation = Conversation() if conversation is None else conversation
        self._on_emotion = on_emotion
```

Replace `_run_turn`, `_produce`, `_enqueue` and `_consume` with:

```python
    async def _run_turn(self, pcm: bytes) -> None:
        # Runs on the event loop after any _interrupt() has called stop(), so this
        # token is fresh; plays from older turns hold stale tokens and write nothing.
        token = self.speaker.current_token()
        turn: _Turn | None = None
        try:
            # An interrupt during THINKING cancels this await but not the transcribe
            # thread: it runs to completion, its result is discarded, and it may briefly
            # compete for CPU with the next turn. Accepted for M1.
            text = await asyncio.to_thread(self.transcriber.transcribe, pcm)
            log.info("STT %.0f ms", self._ms())
            if not text:
                log.info("No speech detected")
                return
            log.info("You: %s", text)
            if is_forget_command(text):
                self.conversation.forget()
                log.info("Memory cleared")
                await self._say(FORGET_LINE, token)
                return
            turn = _Turn(user=text)
            queue: asyncio.Queue[tuple[str, bytes] | None] = asyncio.Queue(maxsize=2)
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self._produce(turn, queue))
                tg.create_task(self._consume(queue, token, turn.spoken))
            turn.complete = True
        except Exception:
            log.exception("Turn failed")
            if turn:
                turn.failed = True
        finally:
            # Also runs when the turn is cancelled by an interrupt.
            if turn:
                self._remember(turn)
            if not asyncio.current_task().cancelling():
                self._set_state(State.IDLE)

    def _remember(self, turn: _Turn) -> None:
        """Keep what the user actually heard; drop failed turns and silent ones."""
        if turn.failed or not turn.spoken:
            return
        reply = f"[{turn.emotion or DEFAULT_EMOTION}] " + " ".join(turn.spoken)
        if not turn.complete:
            reply += f" {INTERRUPTED}"
        self.conversation.add(turn.user, reply)

    async def _produce(self, turn: _Turn, queue: asyncio.Queue[tuple[str, bytes] | None]) -> None:
        """Claude deltas -> emotion + sentences -> PCM, into the queue. None marks the end."""
        parser = EmotionParser()
        splitter = SentenceSplitter()
        reply: list[str] = []
        first_token = True

        async def speak(sentences: list[str]) -> None:
            for sentence in sentences:
                reply.append(sentence)
                await self._enqueue(sentence, queue)

        try:
            history = self.conversation.messages()
            async with contextlib.aclosing(self.chat.stream_reply(history, turn.user)) as deltas:
                async for delta in deltas:
                    if first_token:
                        log.info("First token %.0f ms", self._ms())
                        first_token = False
                    text = parser.feed(delta)
                    self._note_emotion(turn, parser.emotion)
                    await speak(splitter.feed(text))
            text = parser.flush()
            self._note_emotion(turn, parser.emotion)
            await speak(splitter.feed(text) + splitter.flush())
        except LLMRefusal:
            log.warning("Claude refused")
            turn.failed = True
            self._emit_emotion(REFUSAL_LINE.emotion)
            await speak([REFUSAL_LINE.text])
        except LLMError as exc:
            log.warning("LLM error: %s", exc)
            turn.failed = True
            self._emit_emotion(ERROR_LINE.emotion)
            await speak([ERROR_LINE.text])
        log.info("Buddy [%s]: %s", turn.emotion or DEFAULT_EMOTION, " ".join(reply))
        await queue.put(None)

    def _note_emotion(self, turn: _Turn, emotion: str | None) -> None:
        if emotion and turn.emotion is None:
            turn.emotion = emotion
            self._emit_emotion(emotion)

    def _emit_emotion(self, emotion: str) -> None:
        self._on_emotion(emotion)

    async def _say(self, line: Line, token: int) -> None:
        """Speak one canned line (no Claude, nothing remembered)."""
        self._emit_emotion(line.emotion)
        log.info("Buddy [%s]: %s", line.emotion, line.text)
        queue: asyncio.Queue[tuple[str, bytes] | None] = asyncio.Queue()
        await self._enqueue(line.text, queue)
        await queue.put(None)
        await self._consume(queue, token, [])

    async def _enqueue(self, sentence: str, queue: asyncio.Queue[tuple[str, bytes] | None]) -> None:
        pcm = await asyncio.to_thread(self.synthesizer.synthesize, sentence)
        await queue.put((sentence, pcm))

    async def _consume(
        self, queue: asyncio.Queue[tuple[str, bytes] | None], token: int, spoken: list[str]
    ) -> None:
        while (item := await queue.get()) is not None:
            sentence, pcm = item
            if self.state is not State.SPEAKING:
                log.info("First audio %.0f ms", self._ms())
                self._set_state(State.SPEAKING)
            spoken.append(sentence)  # before play: an interrupt mid-sentence still counts it
            await asyncio.to_thread(self.speaker.play, pcm, token)
```

Also check `_run_turn`'s `except Exception`: with `TaskGroup`, a failure arrives as an `ExceptionGroup`, which is a subclass of `Exception`, so it's still caught.

**Step 4: Run the tests and check they pass**

Run: `uv run pytest -q` and `uv run ruff check`
Expected: all pass, lint clean.

**Step 5: Code review**

Dispatch the `python-reviewer` agent on `loop.py` and its tests. The focus is cancellation: `_remember` must run in `finally` on an interrupt, and the `IDLE` state must not flash before the next `LISTENING`. Fix the real findings, then rerun the tests.

**Step 6: Commit**

```bash
git add src/desk_buddy/loop.py tests/test_loop.py
git commit -m "Remember the conversation, report emotions, add forget command"
```

---

### Task 7: Wire memory into the entry point and add the live test

**Files:**
- Modify: `server/src/desk_buddy/__main__.py`
- Modify: `server/tests/integration/test_claude_live.py`

**Step 1: Entry point**

In `_run`, build the conversation and pass it in:

```python
        orchestrator = Orchestrator(
            mic=mic,
            transcriber=transcriber,
            chat=chat,
            synthesizer=synthesizer,
            speaker=speaker,
            conversation=Conversation(settings.history_turns),
        )
```

with `from desk_buddy.brain.memory import Conversation`. `on_emotion` stays at its default for now: the log line already shows the emotion, and the robot link arrives in M4.

**Step 2: Live tests** (add to `integration/test_claude_live.py`; refactor the existing test to use the helper)

```python
import re

from desk_buddy.brain.emotion import EMOTIONS
from desk_buddy.brain.memory import Conversation

LEADING_TAG = re.compile(r"\s*\[(" + "|".join(EMOTIONS) + r")\]")


def live_chat():
    try:
        settings = load_settings()
    except ConfigError:
        pytest.skip("ANTHROPIC_API_KEY not configured")
    return ClaudeChat(
        anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key),
        model=settings.llm_model,
        effort=settings.llm_effort,
        system=system_prompt(settings.max_sentences),
    )


def test_follow_up_question_uses_memory_and_tags():
    chat = live_chat()
    conversation = Conversation()

    async def ask(text):
        reply = "".join([d async for d in chat.stream_reply(conversation.messages(), text)])
        conversation.add(text, reply)
        return reply

    async def run():
        return await ask("Tell me about Labrador Retrievers."), await ask("Are they good hunters?")

    first, second = asyncio.run(run())
    assert LEADING_TAG.match(first), first
    assert LEADING_TAG.match(second), second
    assert any(w in second.lower() for w in ("dog", "lab", "retriev", "bird", "duck")), second
```

**Step 3: Run**

Run: `uv run pytest -q`, `uv run pytest -m integration -q`, `uv run ruff check`
Expected: everything passes, nothing skipped.

**Step 4: Commit**

```bash
git add src/desk_buddy/__main__.py tests/integration/test_claude_live.py
git commit -m "Give the voice loop session memory; live follow-up test"
```

---

### Task 8: Manual check, docs, close M2

**Files:**
- Modify: `docs/decisions.md`, `CLAUDE.md`, `docs/status.md`

**Step 1: Manual checklist** (the user runs `uv run python -m desk_buddy`):
- [ ] "Tell me about Labrador Retrievers", then "Are they good hunters?". The follow-up knows it's about Labs.
- [ ] Interrupt a reply, then ask "What were you saying?". Buddy refers only to what was heard.
- [ ] Say "New topic", hear "Okay, fresh start", then ask "Are they good hunters?". Buddy asks who "they" are.
- [ ] Every turn logs `Buddy [emotion]: …`, with no tag spoken aloud.
- [ ] Replies feel short and calm, with the odd witty line.

**Step 2: `docs/decisions.md`** — add a `## 2026-11-23 — M2 personality` section:
- **Character:** cheerful and calm, occasionally witty; one prompt in `brain/personality.py`.
- **Emotion tags parsed by a streaming parser.** A missing tag means neutral, and stray tags are stripped. · Alternatives: structured output, a separate classification call.
- **Memory until restart, capped by `HISTORY_TURNS` (default 20).** · Alternatives: forget after idle time.
- **Only what was heard is remembered.** Interrupted replies are stored with "(interrupted)"; failed turns and turns interrupted before any speech are dropped.
- **Forget command: fixed phrases checked in code.** · Alternatives: a Claude tool.
- **Short replies are enforced by the prompt only**, so "tell me three facts" still works.

**Step 3: `CLAUDE.md`** — in the Personality section:
- Replace "(name TBD — placeholder **Buddy**)" with "(name TBD; currently **Buddy**). Cheerful and calm, with the odd dry, witty line."
- Add: "Remembers the conversation until restart (last `HISTORY_TURNS` exchanges, only what was actually heard). Saying 'new topic', 'forget that' or 'start over' clears it (fixed phrases in `brain/memory.py`)."
- Change the canned-lines line to say that error, refusal and forget lines exist now, and wake and sleep lines come in M6.

**Step 4: `docs/status.md`** — add a new top section saying M2 is done, with the manual check results and the next milestone (M3, Obsidian dictation).

**Step 5: Verify and commit**

Run: `uv run pytest -q` and `uv run ruff check`

```bash
git add docs/decisions.md CLAUDE.md docs/status.md
git commit -m "Record M2 decisions and close M2"
```
