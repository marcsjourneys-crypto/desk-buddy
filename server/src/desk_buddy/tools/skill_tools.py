"""skill_<slug>: one tool per approved skill (skills_store), run by the SkillRunner.

- The schema is built from the manifest, within Anthropic's strict-schema limits (which
  count across every strict tool in a request): every choice is a required, non-null
  enum (its default is described in words), and the text field is a required string
  where "" means none. So a skill adds no optional or union-typed parameter. Past the
  strict-tool budget, build_tools offers skill tools with strict off: the server checks
  every input itself in any case.
- The server checks everything again: an unknown key or option (case-insensitive), a
  missing required input, text over its limit or with control or format characters is
  refused through the guard.
- The skill's files are fingerprinted again just before every run (and again when a
  confirmed one runs, on the yes); they must still match the approval the tool was built
  from, or the run is refused.
- Every run goes through the ActionGuard (computer control, the limits, the log line, the
  dashboard event). A skill with needs_yes waits in Confirmations for the user's spoken
  yes: "Shall I run <name> with: <text>?". Its text is at most SPOKEN_TEXT characters and
  is sent exactly as it is read back.
- The log and the dashboard show the slug, the choices and the text cut to LOG_TEXT
  characters; never the skill's output (only "Ran <name>.", or "Ran <name> (N links).").
- A skill with `pages` hands back pages (checked by the runner): they join this question's
  SearchResults as a block of their own (first in the next list, numbered in order, a
  second skill's after the first's), run directly or after a yes, so
  open_search_result can open them (by number from the next question on).
"""

from __future__ import annotations

import logging
import unicodedata
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from desk_buddy.skills_store import APPROVED, TEXT_KEY, Manifest, Skill, SkillsStore
from desk_buddy.tools.confirm import RUN, ActionCrashed
from desk_buddy.tools.registry import NotDone, Tool, ToolError
from desk_buddy.tools.urls import MAX_LISTED

log = logging.getLogger(__name__)

SPOKEN_TEXT = 200  # characters of text a needs_yes skill takes: all read back in the question
LOG_TEXT = 60  # characters of the text in the log line and on the dashboard
LOG_VALUE = 40  # characters of a (maybe invalid) choice value there

NOT_AVAILABLE = "{name} isn't available: it changed or was turned off."
UNKNOWN_INPUT = "That skill doesn't take {key}."
MISSING_CHOICE = "That skill needs {choice}: one of {options}."
BAD_CHOICE = "That isn't one of the options for {choice}: {options}."
MISSING_TEXT = "That skill needs its text."
BAD_TEXT = "The text must be plain text."
LONG_TEXT = "The text can be at most {limit} characters."
CONTROL_TEXT = "The text can't hold control or invisible formatting characters."
# Server-written lines after a pages skill's result (outside its untrusted block).
PAGES_FROM = "(Its pages are numbered from {start}.)"
PAGES_FULL = "(Its pages can't be opened: the list of pages is already full.)"
PAGES_STALE = "(Its pages can no longer be opened.)"


class SkillOutput(str):
    """A skill's result for Claude, plus how many pages it handed back (for the log)."""

    links: int = 0


def shown_result(name: str, result: str) -> str:
    """What the log line and the Live tab show of a run: never the output or addresses."""
    links = getattr(result, "links", 0)
    if links:
        return f"Ran {name} ({links} link{'s' if links != 1 else ''})."
    return f"Ran {name}."


class SkillFailed(ToolError):
    """The skill ran (or was meant to) and failed; `summary` is the runner's short
    summary, e.g. "failed (exit 2)"."""

    def __init__(self, message: str, summary: str) -> None:
        super().__init__(message)
        self.summary = summary


def spoken_text(text: str) -> str:
    """The text on one line, whitespace collapsed: what a needs_yes skill gets and what
    the robot reads back (control and format characters are refused before this)."""
    return " ".join(text.split())


def _has_control(text: str) -> bool:
    """Control characters other than newline and tab, or format characters (bidi
    controls, zero-width and tag characters)."""
    for ch in text:
        category = unicodedata.category(ch)
        if (category == "Cc" and ch not in "\n\t") or category == "Cf":
            return True
    return False


# Characters that show or sound like nothing: private use (Co), unassigned (Cn), and the
# Hangul fillers and blank Braille pattern.
INVISIBLE = frozenset("\u115f\u1160\u3164\uffa0\u2800")


def _unheard(text: str) -> bool:
    """Characters a needs_yes skill can't take, because the read-back wouldn't say them."""
    return any(ch in INVISIBLE or unicodedata.category(ch) in ("Co", "Cn") for ch in text)


def _said(text: str) -> str:
    """The text as the question reads it: without a trailing ?, . or !."""
    return text.rstrip("?.!").rstrip()


def text_limit(manifest: Manifest) -> int:
    """The most text the skill takes: its max_length, and for a needs_yes skill at most
    SPOKEN_TEXT, so the question reads all of it back."""
    assert manifest.text is not None
    limit = manifest.text.max_length
    return min(limit, SPOKEN_TEXT) if manifest.needs_yes else limit


def description_for(manifest: Manifest, text: str | None) -> str:
    """What the yes question names: "<name>" or "<name> with: <text>", without a
    trailing ?, . or ! (the question adds its own "?")."""
    said = _said(spoken_text(text)) if text else ""
    return f"{manifest.name} with: {said}" if said else manifest.name


def input_schema(manifest: Manifest) -> dict[str, Any]:
    """The strict schema: every property required, none nullable (no union types)."""
    properties: dict[str, Any] = {}
    for choice in manifest.choices:
        about = choice.description or f"One of: {', '.join(choice.options)}."
        if choice.default is not None:
            about += f' Use "{choice.default}" unless the user says otherwise.'
        properties[choice.name] = {
            "type": "string",
            "enum": list(choice.options),
            "description": about.strip(),
        }
    if manifest.text is not None:
        spec = manifest.text
        about = (
            f'{spec.description} The user\'s own words as they said them (keep "I"; '
            f"add nothing). At most {text_limit(manifest)} characters."
        )
        if not spec.required:
            about += ' Use "" if the user gave none.'
        properties[TEXT_KEY] = {"type": "string", "description": about}
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def tool_description(manifest: Manifest) -> str:
    """The manifest's description (the user's own words, verbatim), then a short note."""
    names = ", ".join(f'"{n}"' for n in manifest.spoken_names)
    text = (
        f'{manifest.description}\n\n(The user\'s skill "{manifest.name}"; they may call it '
        f"{names}.)"
    )
    if manifest.needs_yes:
        text += (
            " It doesn't run at once: after your reply the robot asks the user, and it runs "
            "only if they say yes."
        )
    return text


def _log_result(result: str) -> str:
    """What the tool log line shows of a skill's result: never its output."""
    if isinstance(result, NotDone):
        return "(waiting for a yes)"
    return f"(the skill's output, {len(result)} characters)"


class SkillTools:
    """The skill tools over a snapshot of the usable skills (rebuilt between turns when
    the approvals change). A needs_yes skill is offered only with `confirmations`.
    Pages a skill hands back go into `search_results` (SearchResults), if given."""

    def __init__(
        self,
        guard: Any,
        skills: Iterable[Skill],
        *,
        store: SkillsStore,
        skills_dir: str | Path,
        runner: Any,
        confirmations: Any = None,
        search_results: Any = None,
    ) -> None:
        self._guard = guard
        self._search_results = search_results
        self._store = store
        self._skills_dir = skills_dir
        self._runner = runner
        self._confirmations = confirmations
        self._skills: dict[str, Skill] = {}
        for skill in skills:
            m = skill.manifest
            if m is None or skill.status != APPROVED or skill.fingerprint is None:
                continue
            if m.needs_yes and confirmations is None:
                log.info("Skills: %s needs a yes, which isn't available; not offered", skill.slug)
                continue
            self._skills.setdefault(skill.tool_name, skill)

    @property
    def names(self) -> list[str]:
        return list(self._skills)

    @property
    def has_pages(self) -> bool:
        """Whether any offered skill hands back pages (so open_search_result is needed)."""
        return any(s.manifest is not None and s.manifest.pages for s in self._skills.values())

    def keep_pages(self, search_results: Any) -> None:
        """Where pages go (SearchResults), set once open_search_result is known to be
        offered; until then (or with None) a skill's pages are not kept."""
        self._search_results = search_results

    def tools(self) -> list[Tool]:
        tools = []
        for name, skill in self._skills.items():
            assert skill.manifest is not None
            tools.append(
                Tool(
                    name=name,
                    description=tool_description(skill.manifest),
                    input_schema=input_schema(skill.manifest),
                    func=self._func(skill),
                    log_result=_log_result,
                )
            )
        return tools

    def _func(self, skill: Skill) -> Any:
        def call(**inputs: Any) -> str:
            return self.call(skill, inputs)

        return call

    # --- a call (on a tool thread) ---

    def call(self, skill: Skill, inputs: Mapping[str, Any]) -> str:
        manifest = skill.manifest
        assert manifest is not None
        name = skill.tool_name
        payload, args = self._check(skill, manifest, inputs)
        current = self._approved_now(skill)
        if current is None:
            self._guard.refuse(name, args, NOT_AVAILABLE.format(name=manifest.name))

        def shown(result: str) -> str:
            return shown_result(manifest.name, result)

        if not manifest.needs_yes:
            return self._guard.run(
                name, args, lambda: self._run(current, payload), shown_result=shown
            )
        text = payload.get(TEXT_KEY)
        return self._guard.run_after_yes(
            name,
            args,
            lambda: self._run_confirmed(skill, payload),
            confirmations=self._confirmations,
            description=description_for(manifest, text),
            verb=RUN,
            title=manifest.name,
            ok_note=lambda result: (
                "(System note: the server asked and the user said yes; it ran "
                f"{manifest.name}. Result: {result})"
            ),
            failed_note=lambda exc: _failed_note(manifest.name, exc),
            shown_result=shown,
        )

    def _check(
        self, skill: Skill, manifest: Manifest, inputs: Mapping[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """The inputs as the skill gets them (choices as their listed options, an empty
        text left out), and the arguments for the log and the dashboard. Anything wrong
        is refused through the guard."""
        name = skill.tool_name
        choices: dict[str, Any] = {}
        raw_text = inputs.get(TEXT_KEY)
        args: dict[str, Any] = {"skill": skill.slug, "choices": choices}
        if isinstance(raw_text, str):
            args["text"] = raw_text[:LOG_TEXT]
        known = {c.name for c in manifest.choices}
        if manifest.text is not None:
            known.add(TEXT_KEY)
        for key, value in inputs.items():
            if key not in known:
                self._guard.refuse(name, args, UNKNOWN_INPUT.format(key=str(key)[:LOG_VALUE]))
            if key != TEXT_KEY:
                choices[key] = str(value)[:LOG_VALUE] if value is not None else None
        payload: dict[str, Any] = {}
        for choice in manifest.choices:
            value = inputs.get(choice.name)
            options = ", ".join(choice.options)
            if value is None:  # a non-strict call may leave it out
                if choice.default is None:
                    message = MISSING_CHOICE.format(choice=choice.name, options=options)
                    self._guard.refuse(name, args, message)
                value = choice.default
            picked = None
            if isinstance(value, str):
                picked = next((o for o in choice.options if o == value.strip().lower()), None)
            if picked is None:
                message = BAD_CHOICE.format(choice=choice.name, options=options)
                self._guard.refuse(name, args, message)
            payload[choice.name] = choices[choice.name] = picked
        if manifest.text is not None:
            text = self._check_text(skill, manifest, raw_text, args)
            if text:
                payload[TEXT_KEY] = text
        return payload, args

    def _check_text(
        self, skill: Skill, manifest: Manifest, raw: Any, args: Mapping[str, Any]
    ) -> str:
        name = skill.tool_name
        assert manifest.text is not None
        if raw is not None and not isinstance(raw, str):
            self._guard.refuse(name, args, BAD_TEXT)
        text = (raw or "").replace("\r\n", "\n").strip()
        if _has_control(text):
            self._guard.refuse(name, args, CONTROL_TEXT)
        if manifest.needs_yes:
            if _unheard(text):
                self._guard.refuse(name, args, CONTROL_TEXT)
            text = spoken_text(text)  # exactly what the robot reads back
            if text and not _said(text):  # only "?", "." or "!": nothing to read back
                self._guard.refuse(name, args, MISSING_TEXT)
        if not text and manifest.text.required:
            self._guard.refuse(name, args, MISSING_TEXT)
        limit = text_limit(manifest)
        if len(text) > limit:
            self._guard.refuse(name, args, LONG_TEXT.format(limit=limit))
        return text

    def _approved_now(self, skill: Skill) -> Skill | None:
        """The skill as it is on disk right now, if it's still approved with the files
        this tool was built from; otherwise None."""
        try:
            current = self._store.check(self._skills_dir, skill.slug)
        except Exception:
            log.warning("Skills: couldn't check %s", skill.slug, exc_info=True)
            return None
        if (
            current is None
            or current.status != APPROVED
            or current.manifest is None
            or current.fingerprint != skill.fingerprint
        ):
            log.warning("Skills: %s changed or was turned off; not run", skill.slug)
            return None
        return current

    def _run(self, skill: Skill, payload: Mapping[str, Any]) -> str:
        try:
            result = self._runner.run(skill, payload)
        except Exception as exc:
            log.exception("Skills: %s crashed the runner", skill.slug)
            raise SkillFailed("The skill failed unexpectedly.", "failed unexpectedly") from exc
        if not result.ok:
            raise SkillFailed(result.text, result.summary)
        pages = tuple(getattr(result, "pages", ()) or ())
        if not pages or self._search_results is None:
            return SkillOutput(result.text)
        # First in the next list, after any earlier skill's pages in this question; a
        # tool thread from a question that has moved on adds nothing.
        start = self._search_results.add_skill_pages(pages)
        text = result.text
        if start is None:
            text += "\n" + PAGES_STALE
        elif start > MAX_LISTED:
            text += "\n" + PAGES_FULL
        elif start > 1:
            text += "\n" + PAGES_FROM.format(start=start)
        output = SkillOutput(text)
        if start is not None:
            output.links = sum(1 for _, _, url in pages if url)
        return output

    def _run_confirmed(self, skill: Skill, payload: Mapping[str, Any]) -> str:
        """On the user's yes: check the files again at this moment, then run."""
        current = self._approved_now(skill)
        if current is None:
            assert skill.manifest is not None
            raise ToolError(NOT_AVAILABLE.format(name=skill.manifest.name))
        return self._run(current, payload)


def _failed_note(name: str, exc: ToolError) -> str:
    if isinstance(exc, SkillFailed):
        how = exc.summary if exc.summary.startswith("failed") else f"wasn't run ({exc.summary})"
        return (
            f"(System note: the server asked and the user said yes; it tried to run {name}, "
            f"but it {how}.)"
        )
    if isinstance(exc, ActionCrashed):
        return (
            f"(System note: the server asked and the user said yes; running {name} failed "
            "unexpectedly, and it may not have run.)"
        )
    return f"(System note: the server asked and the user said yes, but nothing was run: {exc})"
