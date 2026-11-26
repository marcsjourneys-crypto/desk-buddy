# Smarter model when needed: design and plan

Approved by the user on 2026-11-26.

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** CARL stays on Haiku, but a hard question, or one where the user says "think hard", is answered by a stronger model (Sonnet 5.5 by default), which stays on for the follow-up questions right after.

**Architecture:**
- `brain/smart.py` holds the spoken phrases and a small router that remembers the 2-minute stay.
- The loop asks the router before each reply and tells `ClaudeChat` which route to take.
- `ClaudeChat` runs the turn on the smart model, or on Haiku with a `think_harder` tool in its first round. If Haiku calls that tool, its round is thrown away and the turn runs again on the smart model.
- A failed smart call falls back to Haiku. A model the account can't use turns the feature off until restart, the same way web search does.

**Tech stack:** no new dependencies. The `anthropic` SDK is already in use, and the tests use the fake streaming client in `tests/test_llm.py`.

---

## Decisions (from the brainstorm)

- **Both ways in:**
  - A fixed spoken phrase always uses the smart model. The server checks the transcript itself, so no Haiku round comes first.
  - Haiku may also hand a question over with the `think_harder` tool.
- **The default is Sonnet 5.5** (`claude-sonnet-5-5`), set with `SMART_MODEL` in a new "Thinking" group on the dashboard. `off` turns the feature off. Blank or missing means the default, because `.env.example` keeps its values blank.
- **It stays on through follow-ups.** After a smart answer, the next question also goes to the smart model if it comes within 2 minutes (`STAY_SECONDS = 120`). Each smart answer restarts the 2 minutes, and a forget or a restart ends it.
- **Silent:** no spoken line. The eyes show thinking, and `PONDERING` still plays after 4 s. The Live tab labels each smart reply with the model and the reason: `asked`, `handed over`, `follow-on`, or `unavailable` (a Haiku reply after the smart model failed).
- **Same CARL:** the same system prompt (without the `think_harder` rule), history, tools, guard, spoken yes and short spoken style. Only the model, the effort and `max_tokens` change.
- **Thinking:** Sonnet 5.5's adaptive thinking is on by default, so `thinking` is left out, as it is for Haiku today. Thinking text is never spoken, logged or stored. The loop already sends each round's content back unchanged, thinking blocks included, and memory only stores plain `tool_use`/`tool_result` and text.
  - Effort is `high` when the user asked, `medium` for a handover or a follow-on (spoken replies shouldn't wait too long).
  - `SMART_MAX_TOKENS = 8192`, because thinking counts toward it.
- **The handover is only offered in Haiku's first round,** so it can never repeat a tool that already ran.
  - If Haiku's first round stops on `tool_use` and `think_harder` is among the calls, none of that round's tools run, and the turn starts again on the smart model with the same messages and a fresh 3 rounds. The handover's own blocks are never stored.
  - Text Haiku already spoke stands (the prompt says to call it with no words first).
  - Searches already reported stay reported.
  - `think_harder` isn't a computer-control action: no guard, no yes, no counting, and no `TASK_DONE`. It's non-strict and has no inputs, so it doesn't count toward the 20 strict tools.
- **Failures on the smart model:**
  - An API or network error **before any text was yielded and before any tool round** falls back to Haiku for that question, without `think_harder`, and logs a warning. The route becomes `unavailable`.
  - If the cause was `anthropic.NotFoundError` or `anthropic.PermissionDeniedError` (a wrong model name, or the account can't use it), it also logs a hint once and blocks the smart model until restart (a module-level flag like `_web_search_blocked`).
  - A refusal, or an error after text or tools, is handled as today (the error line).
- **Web search** stays offered on smart turns with the same tool version. If Sonnet 5.5 refuses `web_search_20250305` with a 400, the existing retry-without-search path handles it. The optional live test (Task 5) checks this.
- **Cost:** Sonnet 5.5 is $2 / $10 per MTok against Haiku 5.5's $0.10 / $0.50. One smart question with CARL's prompt and some thinking is about 2 to 5 cents. The 2-minute stay is the main thing that drives it.

## Tasks

### Task 1: The `SMART_MODEL` setting

**Files:**
- Modify `src/desk_buddy/config.py`:
  - add `"SMART_MODEL"` to `ENV_KEYS` and `DASHBOARD_KEYS`;
  - add `smart_model: str = "claude-sonnet-5-5"` to `Settings`, where `""` means off;
  - add a `_smart_model(get)` parser: lowercase `off` gives `""`, anything else must fullmatch `_LLM_MODEL`, and blank falls back to the default through `get` (which already does this).
- Modify `src/desk_buddy/settings_store.py`: add `Editable("SMART_MODEL", "Thinking", NEXT_QUESTION, "text", "Model for hard questions (off = Haiku only)")` after the Brain group.
- Modify `server/.env.example`: add `SMART_MODEL=` with a comment saying `off` turns it off and blank means `claude-sonnet-5-5`.
- Tests in `tests/test_config.py` and `tests/test_settings_store.py`:
  - the default;
  - `off`, `OFF` and ` off ` give `""`;
  - a valid id is kept;
  - `bad model!` is a ConfigError;
  - it's editable on the dashboard and listed in the Thinking group.

Run `uv run pytest tests/test_config.py tests/test_settings_store.py -q`, then the full suite and `uv run ruff check`.

**Commit:** `Add the SMART_MODEL setting`

### Task 2: Phrases and the stay (`brain/smart.py`)

**Files:** create `src/desk_buddy/brain/smart.py` and `tests/test_smart.py`.

```python
"""When a question goes to the smart model: a spoken phrase, or the 2-minute stay after
a smart answer. The handover by Haiku itself lives in llm.py (the think_harder tool)."""

import re
import time
from collections.abc import Callable

STAY_SECONDS = 120.0  # after a smart answer, the next question within this stays smart

# Word sequences that ask for the smart model, anywhere in what the user said.
SMART_PHRASES: tuple[tuple[str, ...], ...] = (
    ("think", "hard"),
    ("think", "harder"),
    ("think", "carefully"),
    ("think", "deeply"),
    ("think", "it", "through"),
    ("big", "brain"),
)
_WORD = re.compile(r"[a-z0-9']+")

ASKED = "asked"
HANDED_OVER = "handed over"
FOLLOW_ON = "follow-on"
UNAVAILABLE = "unavailable"


def wants_smart(text: str) -> bool:
    words = _WORD.findall(text.lower())
    return any(
        tuple(words[i : i + len(p)]) == p for p in SMART_PHRASES for i in range(len(words))
    )


class SmartRouter:
    """Remembers the stay between turns. Owned by the loop (the chat is rebuilt per turn)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._until: float | None = None

    def route(self, text: str) -> str | None:
        if wants_smart(text):
            return ASKED
        if self._until is not None and self._clock() < self._until:
            return FOLLOW_ON
        return None

    def answered(self, smart: bool) -> None:
        """After a reply (also one cut off): a smart one restarts the stay, a Haiku one ends it."""
        self._until = self._clock() + STAY_SECONDS if smart else None

    def forget(self) -> None:
        self._until = None
```

Tests:
- each phrase, in a sentence and in any case ("Can you THINK HARD about this?");
- "I think hardware is cool", "brainy" and "think" alone don't match;
- with a fake clock: no route at first; `follow-on` 119 s after a smart answer and none at 121 s;
- a Haiku answer ends the stay;
- `forget` ends it;
- a phrase gives `asked` even during a stay.

**Commit:** `Add the smart-model phrases and stay`

### Task 3: Smart runs, fallback and the block in `ClaudeChat`

**Files:** modify `src/desk_buddy/llm.py`; tests in `tests/test_llm.py` (reuse its fake client: check how it records `params`).

- **Inputs:** `ClaudeChat.__init__` gains:
  - `smart_model: str | None = None`;
  - `plain_system: str | Callable[[], str] | None = None`, the prompt without the `think_harder` rule, defaulting to `system`.
- **Attributes:**
  - `smart_route: str | None = None`, set by the loop before `stream_reply`: `"asked"`, `"follow-on"` or None;
  - `answered_by: tuple[str, str] | None`, (model, route) after a reply that used or tried the smart model, reset to None at the start of each reply.
- **Module-level `_smart_blocked`** with `smart_blocked()` and `_block_smart(hint)`, like `_web_search_blocked`. The hint is logged once: `"SMART_MODEL %r isn't available to this API key; answering on LLM_MODEL until restart (set SMART_MODEL=off to silence this)"`.
- **The `smart_available` property:** `smart_model` is set, isn't blocked, and differs from `model`.
- **Refactor** the body of `stream_reply` into `_rounds(messages, *, model, effort, max_tokens, handover)`, an async generator with the same behaviour. `_params` takes the model, effort and `max_tokens` instead of reading them from `self`. Keep every existing test passing unchanged; this refactor is part of the commit.
- **`stream_reply`:**
  1. If `smart_route` is set and `smart_available`: log `Model: <smart> (<route>)` and run the rounds on the smart model, with effort `high` for asked or `medium` otherwise, `SMART_MAX_TOKENS` and `plain_system`. Set `answered_by = (smart, route)`.
  2. On `LLMError` (not `LLMRefusal`) from that run, if nothing was yielded and `last_tool_rounds` is empty:
     - log `Smart model failed (%s); answering on <model>`;
     - if `exc.__cause__` is `anthropic.NotFoundError` or `PermissionDeniedError`, block the smart model;
     - set `answered_by = (model, "unavailable")`;
     - run on Haiku with `handover=False`.
     Otherwise, re-raise.
  3. With no smart route: run on Haiku (`self._effort`, `MAX_TOKENS`, `system`), with `handover = smart_available`.
- **Tests:**
  - with no smart model, the params are identical to today: same model and `max_tokens`, no extra tool;
  - an asked route uses the smart model with effort `high`, 8192 tokens and the plain system prompt;
  - a follow-on route uses effort `medium`;
  - a route is ignored when `smart_model` is None or blocked;
  - an `APIConnectionError` before any text falls back to Haiku, the reply still streams, and `answered_by` is `unavailable`;
  - a `NotFoundError` blocks the smart model (the next chat object ignores the route) and logs the hint once;
  - an error after text was yielded raises `LLMError` with no fallback;
  - a refusal never falls back.
- Add an autouse fixture that resets `_smart_blocked` (look at how the web-search block is reset in the tests).

**Commit:** `Answer on the smart model when asked, falling back to Haiku`

### Task 4: The `think_harder` handover and its prompt

**Files:** modify `src/desk_buddy/llm.py` (including `prompt_for` and `create_chat`) and `src/desk_buddy/brain/personality.py`; tests in `tests/test_llm.py` and `tests/test_personality.py`.

- **The tool schema** (`THINK_HARDER_TOOL`, not strict, not in `ToolRegistry`):
  ```python
  {"name": "think_harder",
   "description": "Hand this question to CARL's deeper-thinking model. Call it alone, "
                  "first, with no text before it.",
   "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}}
  ```
- **When it's offered:** in `_params`, only when `handover` is set and it's round 1 (never on the last round, never with `tool_choice: none`).
- **The handover:**
  - In `_rounds`: on `stop_reason == "tool_use"` in round 1 with `handover`, if any `tool_use` is named `think_harder`, log `Handing over to <smart>` and raise a private `_HandOver` before running any tool or recording `last_tool_rounds`.
  - In `stream_reply`: catch it, set `answered_by = (smart, "handed over")`, and run the smart rounds with effort `medium` on the original messages. `spoke` carries over, so a `"\n"` separates anything Haiku already said.
  - A failure of that smart run follows Task 3's fallback rule (Haiku again, `handover=False`).
- **The prompt:** add `THINK_HARDER_TEXT` to `brain/personality.py`, with `system_prompt(..., think_harder: bool = False)` appending it:
  > "You can hand a question to your deeper-thinking model with the think_harder tool. Use it for maths, code, puzzles, careful multi-step reasoning, comparing complicated options, or when you aren't confident you can answer well. Don't use it for small talk, notes, music, opening things, skills or simple facts. Call it straight away, alone, with no words before it; the deeper model answers in your voice."
- **`prompt_for(..., think_harder=False)`** passes it through. `create_chat` gets the smart model from settings and builds `system` with `think_harder=True` (only when the setting is on and not blocked) and `plain_system` with `think_harder=False`. The dashboard's prompt preview keeps calling `prompt_for` without it.
- **Tests:**
  - the tool is only in round 1's params and only on a Haiku run that may hand over (not on a smart run, a fallback, or with the feature off);
  - Haiku calling `think_harder` makes the next request use the smart model with the original messages (no `think_harder` `tool_use` in them), and `answered_by` is `handed over`;
  - a `think_harder` call next to another tool runs neither, and `tools_succeeded` is unchanged;
  - Haiku text before the call is kept, with `"\n"` before Sonnet's text;
  - `last_tool_rounds` holds only the smart run's rounds;
  - the prompt contains `THINK_HARDER_TEXT` only with `think_harder=True`.

**Commit:** `Let Haiku hand hard questions to the smart model`

### Task 5: The loop, the Live tab, and an optional live test

**Files:**
- Modify `src/desk_buddy/loop.py` and its constructor.
- Modify `src/desk_buddy/__main__.py`, so one `SmartRouter` lives for the whole process.
- Modify `src/desk_buddy/dashboard_static/` (the JS that renders `reply` events).
- Tests in `tests/test_loop.py`, `tests/test_dashboard_page.py` and `tests/integration/test_smart_model_live.py`.

- **In the turn:**
  - after the forget check and before the reply, `route = router.route(text)`, and `chat.smart_route = route` when the chat has that attribute;
  - after the reply (in the `finally`), if the turn had a reply (`turn.spoken`) or completed without failing, call `router.answered(answered_by is not None and answered_by[1] != "unavailable")`, so a cut-off smart reply still keeps the stay, and a failed turn leaves it unchanged;
  - a forget calls `router.forget()`.
- **The `reply` event** gains `model` and `route` when `answered_by` is set (fields left out otherwise). The Live tab shows a small label after CARL's line with `textContent`: the model id without `claude-`, then ` · ` and the route, for example `sonnet-5-5 · asked`.
- **Tests:**
  - with a fake chat, "think hard about X" sets `asked`;
  - the next question within the stay sets `follow-on`;
  - a forget clears it;
  - a failed turn doesn't start a stay;
  - the reply event carries the fields only when set;
  - the page renders the label with `textContent` (follow the existing page tests).
- **The live test** (marked `integration`, skipped without `ANTHROPIC_API_KEY`, **never run without asking the user**, a few cents): one asked question on `claude-sonnet-5-5` with web search offered, which streams some text; one Haiku question with a hard maths prompt, where we print whether it handed over (informational, not asserted).

**Commit:** `Route questions to the smart model and show it on the Live tab`

### Task 6: Docs

- **`CLAUDE.md`:**
  - the LLM bullet: the smart model, the phrases, the handover, the stay, the fallback and the block;
  - the dashboard's Thinking group;
  - the roadmap: this feature, plus **Skill builder (later)**: the user types or says what they want, the smart model writes the skill (`skill.json` and `main.py`) into `SKILLS_DIR`, and the user approves it on the Apps tab as usual, so nothing runs unreviewed.
- **`server/README.md`:** a short "Smarter answers" section with the phrases, the setting, `off`, and the cost.
- **`docs/decisions.md`** and **`docs/status.md`** (next: the user check).

**Commit:** `Document the smart model`

## User check

1. Restart CARL. "Hey CARL, think hard: what's 17 times 23, minus the square root of 144?" The Live tab shows `sonnet-5-5 · asked`, and the answer is right.
2. Within 2 minutes, "and divided by 7?" shows `follow-on`.
3. After more than 2 minutes, "what time is it?" has no label (Haiku).
4. "Hey CARL, explain how a bloom filter works and when I'd use one." Haiku may hand it over (`handed over`); whether it does is Haiku's call.
5. Set `SMART_MODEL` to `off` on the dashboard. "Think hard about X" has no label.
