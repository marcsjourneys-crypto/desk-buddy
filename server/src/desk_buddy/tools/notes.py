"""Buddy's Obsidian vault: dated notes and ideas. Only appends and creates, never edits."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path

from desk_buddy.tools.registry import ToolError

KINDS = ("note", "idea")
MAX_TITLE = 60
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_title(raw: str) -> str:
    """A title that is safe as part of a Windows filename (may be empty)."""
    title = " ".join(_UNSAFE.sub(" ", raw).split())
    title = re.sub(r"\.{2,}", "", title).strip(" .")
    return title[:MAX_TITLE].rstrip(" .")


def day_heading(day: date) -> str:
    return f"{day:%A} {day.day} {day:%B %Y}"


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

    # --- paths ---

    def _inside(self, folder: str, name: str) -> Path:
        path = (self._root / folder / name).resolve()
        if not path.is_relative_to(self._root):
            raise ToolError("That would be outside the notes folder.")
        return path
