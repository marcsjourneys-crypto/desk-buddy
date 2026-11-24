"""Buddy's Obsidian vault: dated notes and ideas. Only appends and creates, never edits."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path

from desk_buddy.tools.registry import Tool, ToolError

KINDS = ("note", "idea")
MAX_TITLE = 60
MAX_READ = 4000
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_title(raw: str) -> str:
    """A title that is safe as part of a Windows filename (may be empty)."""
    title = " ".join(_UNSAFE.sub(" ", raw).split())
    title = re.sub(r"\.{2,}", "", title).strip(" .")
    return title[:MAX_TITLE].rstrip(" .")


def day_heading(day: date) -> str:
    return f"{day:%A} {day.day} {day:%B %Y}"


def _idea_body(content: str) -> str:
    """The idea's text without frontmatter or heading, on one line."""
    if content.startswith("---\n"):
        end = content.find("\n---\n", 4)
        if end != -1:
            content = content[end + 5 :]
    lines = [ln for ln in content.splitlines() if not ln.startswith("# ")]
    return " ".join(" ".join(lines).split())


class NoteVault:
    def __init__(
        self,
        root: Path,
        *,
        daily_folder: str = "Daily",
        ideas_folder: str = "Ideas",
        now: Callable[[], datetime] = datetime.now,
    ) -> None:
        self._root = Path(root).resolve()
        self._daily = daily_folder
        self._ideas = ideas_folder
        self._now = now

    # --- take_note ---

    def take_note(self, text: str, kind: str, title: str) -> str:
        text = " ".join(text.split())
        if not text:
            raise ToolError("There's nothing to note.")
        if kind not in KINDS:
            raise ToolError(f"kind must be one of {', '.join(KINDS)}.")
        now = self._now()
        if kind == "note":
            return self._append_note(now, text)
        return self._create_idea(now, text, title)

    def _append_note(self, now: datetime, text: str) -> str:
        name = f"{now.date().isoformat()}.md"
        path = self._inside(self._daily, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        prefix = ""
        if not path.exists():
            prefix = f"# {day_heading(now.date())}\n\n"
        elif not path.read_text(encoding="utf-8").endswith("\n"):
            prefix = "\n"
        with path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(f"{prefix}- {now:%H:%M} {text}\n")
        return f"Saved to {self._daily}/{name}"

    def _create_idea(self, now: datetime, text: str, title: str) -> str:
        title = safe_title(title) or safe_title(" ".join(text.split()[:6])) or "Idea"
        day = now.date().isoformat()
        body = f"---\ndate: {day}\ntags: [idea, buddy]\n---\n# {title}\n\n{text}\n"
        for n in range(1, 1000):
            name = f"{day} {title}.md" if n == 1 else f"{day} {title} {n}.md"
            path = self._inside(self._ideas, name)
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with path.open("x", encoding="utf-8", newline="\n") as f:
                    f.write(body)
            except FileExistsError:
                continue
            return f"Saved idea to {self._ideas}/{name}"
        raise ToolError("Too many ideas with that title today.")

    # --- read_notes ---

    def read_notes(self, date: str) -> str:
        try:
            day = datetime.strptime(date, "%Y-%m-%d").date()
        except ValueError:
            raise ToolError("date must look like 2026-11-23.") from None
        iso = day.isoformat()
        notes: list[str] = []
        daily = self._inside(self._daily, f"{iso}.md")
        if daily.is_file():
            notes = [
                line
                for line in daily.read_text(encoding="utf-8").splitlines()
                if line.startswith("- ")
            ]
        ideas: list[str] = []
        ideas_dir = self._inside(self._ideas, "")
        if ideas_dir.is_dir():
            for path in sorted(ideas_dir.glob(f"{iso} *.md")):
                title = path.stem[len(iso) + 1 :]
                ideas.append(f"- {title}: {_idea_body(path.read_text(encoding='utf-8'))}")
        if not notes and not ideas:
            return f"Nothing noted on {day_heading(day)}."
        parts = [f"Notes for {day_heading(day)}:", *notes]
        if ideas:
            parts += ["Ideas:", *ideas]
        text = "\n".join(parts)
        if len(text) > MAX_READ:
            text = text[: MAX_READ - 20].rstrip() + "\n(and more)"
        return text

    # --- paths ---

    def _inside(self, folder: str, name: str) -> Path:
        path = (self._root / folder / name).resolve()
        if not path.is_relative_to(self._root):
            raise ToolError("That would be outside the notes folder.")
        return path


def note_tools(vault: NoteVault) -> list[Tool]:
    return [
        Tool(
            name="take_note",
            description=(
                "Save something the user dictated to their notes. kind 'note' adds a "
                "timestamped line to today's daily note; kind 'idea' creates a new idea "
                "file and needs a short title (2-6 words). For a note, title is an empty "
                "string. Lightly clean the text first: fix obvious mishearings, punctuation "
                "and filler words, but keep the user's own wording."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "The cleaned-up note text."},
                    "kind": {"type": "string", "enum": list(KINDS)},
                    "title": {"type": "string", "description": "Idea title, or empty."},
                },
                "required": ["text", "kind", "title"],
                "additionalProperties": False,
            },
            func=vault.take_note,
        ),
        Tool(
            name="read_notes",
            description=(
                "Read back the notes and ideas the user saved on one day. Use today's "
                "date (given in the system prompt) unless they name another day."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "The day, as YYYY-MM-DD."},
                },
                "required": ["date"],
                "additionalProperties": False,
            },
            func=vault.read_notes,
        ),
    ]
