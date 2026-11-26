"""skill_<slug>: one tool per approved skill (skills_store), run by the SkillRunner.

- The schema is built from the manifest: each choice an enum (a choice with a default may
  be null, and the server fills the default in), and the optional text field a string.
  The server checks everything again: an unknown key or option, a missing required input,
  text over its max_length or with control characters is refused through the guard.
- The skill's files are fingerprinted again just before every run (and again when a
  confirmed one runs, on the yes): a skill that changed or was turned off is refused.
- Every run goes through the ActionGuard (computer control, the limits, the log line, the
  dashboard event). A skill with needs_yes waits in Confirmations for the user's spoken
  yes: "Shall I run <name> with: <text>?".
- The log and the dashboard show the slug, the choices and the text cut to LOG_TEXT
  characters; never the skill's output (only "Ran <name>.").
"""

from __future__ import annotations

import logging
import unicodedata
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from desk_buddy.skills_store import APPROVED, TEXT_KEY, Manifest, Skill, SkillsStore
from desk_buddy.tools.confirm import RUN
from desk_buddy.tools.registry import Tool, ToolError

log = logging.getLogger(__name__)

SPOKEN_TEXT = 200  # characters of the text read back in the robot's question
LOG_TEXT = 60  # characters of the text in the log line and on the dashboard
LOG_VALUE = 40  # characters of a (maybe invalid) choice value there

NOT_AVAILABLE = "{name} isn't available: it changed or was turned off."
UNKNOWN_INPUT = "That skill doesn't take {key}."
MISSING_CHOICE = "That skill needs {choice}: one of {options}."
BAD_CHOICE = "That isn't one of the options for {choice}: {options}."
MISSING_TEXT = "That skill needs its text."
BAD_TEXT = "The text must be plain text."
LONG_TEXT = "The text can be at most {limit} characters."
CONTROL_TEXT = "The text can't hold control characters."


class SkillFailed(ToolError):
    """The skill ran (or was meant to) and failed; `summary` is the runner's short
    summary, e.g. "failed (exit 2)"."""

    def __init__(self, message: str, summary: str) -> None:
        super().__init__(message)
        self.summary = summary


def spoken_text(text: str, limit: int = SPOKEN_TEXT) -> str:
    """The text as the robot reads it back: one line, no control or format characters,
    whitespace collapsed, cut to `limit` characters."""
    kept = "".join(" " if unicodedata.category(ch) in ("Cc", "Cf") else ch for ch in text)
    return " ".join(kept.split())[:limit].strip()


def _has_control(text: str) -> bool:
    return any(unicodedata.category(ch) == "Cc" and ch not in "\n\t" for ch in text)


def description_for(manifest: Manifest, text: str | None) -> str:
    """What the yes question names: "<name>" or "<name> with: <text>"."""
    said = spoken_text(text) if text else ""
    return f"{manifest.name} with: {said}" if said else manifest.name


def input_schema(manifest: Manifest) -> dict[str, Any]:
    """The strict schema: every property listed in required; optional ones nullable."""
    properties: dict[str, Any] = {}
    for choice in manifest.choices:
        options: list[Any] = list(choice.options)
        about = choice.description or f"One of: {', '.join(choice.options)}."
        if choice.default is None:
            prop: dict[str, Any] = {"type": "string", "enum": options}
        else:
            about += f' Null means the default, "{choice.default}".'
            prop = {"type": ["string", "null"], "enum": [*options, None]}
        prop["description"] = about.strip()
        properties[choice.name] = prop
    if manifest.text is not None:
        spec = manifest.text
        about = f"{spec.description} At most {spec.max_length} characters."
        if spec.required:
            properties[TEXT_KEY] = {"type": "string", "description": about}
        else:
            properties[TEXT_KEY] = {
                "type": ["string", "null"],
                "description": about + " Null if the user gave none.",
            }
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


class SkillTools:
    """The skill tools over a snapshot of the usable skills (rebuilt between turns when
    the approvals change). A needs_yes skill is offered only with `confirmations`."""

    def __init__(
        self,
        guard: Any,
        skills: Iterable[Skill],
        *,
        store: SkillsStore,
        skills_dir: str | Path,
        runner: Any,
        confirmations: Any = None,
    ) -> None:
        self._guard = guard
        self._store = store
        self._skills_dir = skills_dir
        self._runner = runner
        self._confirmations = confirmations
        self._skills: dict[str, Skill] = {}
        for skill in skills:
            m = skill.manifest
            if m is None or skill.status != APPROVED:
                continue
            if m.needs_yes and confirmations is None:
                log.info("Skills: %s needs a yes, which isn't available; not offered", skill.slug)
                continue
            self._skills.setdefault(skill.tool_name, skill)

    @property
    def names(self) -> list[str]:
        return list(self._skills)

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
        done = f"Ran {manifest.name}."
        if not manifest.needs_yes:
            return self._guard.run(
                name, args, lambda: self._run(current, payload), shown_result=lambda _r: done
            )
        text = payload.get(TEXT_KEY)
        return self._guard.run_after_yes(
            name,
            args,
            lambda: self._run_confirmed(skill, payload),
            confirmations=self._confirmations,
            description=description_for(manifest, text),
            verb=RUN,
            ok_note=lambda result: (
                "(System note: the server asked and the user said yes; it ran "
                f"{manifest.name}. Result: {result})"
            ),
            failed_note=lambda exc: _failed_note(manifest.name, exc),
            shown_result=lambda _r: done,
        )

    def _check(
        self, skill: Skill, manifest: Manifest, inputs: Mapping[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """The inputs as the skill gets them (defaults filled in, empty optional text
        left out), and the arguments for the log and the dashboard. Anything wrong is
        refused through the guard."""
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
            if value is None:
                if choice.default is None:
                    message = MISSING_CHOICE.format(choice=choice.name, options=options)
                    self._guard.refuse(name, args, message)
                value = choice.default
            if not isinstance(value, str) or value not in choice.options:
                message = BAD_CHOICE.format(choice=choice.name, options=options)
                self._guard.refuse(name, args, message)
            payload[choice.name] = choices[choice.name] = value
        spec = manifest.text
        if spec is not None:
            if raw_text is not None and not isinstance(raw_text, str):
                self._guard.refuse(name, args, BAD_TEXT)
            text = (raw_text or "").replace("\r\n", "\n").strip()
            if not text:
                if spec.required:
                    self._guard.refuse(name, args, MISSING_TEXT)
            elif _has_control(text):
                self._guard.refuse(name, args, CONTROL_TEXT)
            elif len(text) > spec.max_length:
                self._guard.refuse(name, args, LONG_TEXT.format(limit=spec.max_length))
            else:
                payload[TEXT_KEY] = text
        return payload, args

    def _approved_now(self, skill: Skill) -> Skill | None:
        """The skill as it is on disk right now, if it's still approved with these files;
        otherwise None."""
        try:
            current = self._store.check(self._skills_dir, skill.slug)
        except Exception:
            log.warning("Skills: couldn't check %s", skill.slug, exc_info=True)
            return None
        if current is None or current.status != APPROVED or current.manifest is None:
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
        return result.text

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
    return f"(System note: the server asked and the user said yes, but nothing was run: {exc})"
