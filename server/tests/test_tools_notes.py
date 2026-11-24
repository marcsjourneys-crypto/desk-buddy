from datetime import datetime

import pytest

from desk_buddy.tools.notes import NoteVault, safe_title
from desk_buddy.tools.registry import ToolError

NOW = datetime(2026, 11, 23, 16, 38)


@pytest.fixture
def vault(tmp_path):
    return NoteVault(tmp_path, now=lambda: NOW)


def read(path):
    return path.read_text(encoding="utf-8")


def test_first_note_creates_daily_file_with_heading(vault, tmp_path):
    assert vault.take_note("Pick up dog food", "note", "") == "Saved to Daily/2026-11-23.md"
    assert read(tmp_path / "Daily" / "2026-11-23.md") == (
        "# Monday 23 November 2026\n\n- 16:38 Pick up dog food\n"
    )


def test_later_notes_append(vault, tmp_path):
    vault.take_note("one", "note", "")
    vault.take_note("two", "note", "")
    assert read(tmp_path / "Daily" / "2026-11-23.md").endswith("- 16:38 one\n- 16:38 two\n")


def test_append_after_file_without_trailing_newline(vault, tmp_path):
    daily = tmp_path / "Daily"
    daily.mkdir()
    (daily / "2026-11-23.md").write_text("# Mine", encoding="utf-8")
    vault.take_note("x", "note", "")
    assert read(daily / "2026-11-23.md") == "# Mine\n- 16:38 x\n"


def test_multi_line_note_becomes_one_line(vault, tmp_path):
    vault.take_note("first\n  second\tthird", "note", "")
    assert "- 16:38 first second third\n" in read(tmp_path / "Daily" / "2026-11-23.md")


@pytest.mark.parametrize("text", ["", "   \n"])
def test_empty_note_is_rejected(vault, text):
    with pytest.raises(ToolError):
        vault.take_note(text, "note", "")


def test_unknown_kind_is_rejected(vault):
    with pytest.raises(ToolError):
        vault.take_note("x", "shopping", "")


def test_idea_creates_file_with_frontmatter(vault, tmp_path):
    result = vault.take_note("A lamp that dims when Buddy sleeps.", "idea", "Dimming lamp")
    assert result == "Saved idea to Ideas/2026-11-23 Dimming lamp.md"
    assert read(tmp_path / "Ideas" / "2026-11-23 Dimming lamp.md") == (
        "---\ndate: 2026-11-23\ntags: [idea, buddy]\n---\n"
        "# Dimming lamp\n\nA lamp that dims when Buddy sleeps.\n"
    )


def test_duplicate_idea_title_gets_a_number(vault, tmp_path):
    vault.take_note("a", "idea", "Lamp")
    assert vault.take_note("b", "idea", "Lamp") == "Saved idea to Ideas/2026-11-23 Lamp 2.md"
    assert vault.take_note("c", "idea", "Lamp") == "Saved idea to Ideas/2026-11-23 Lamp 3.md"


def test_idea_without_title_uses_first_words(vault, tmp_path):
    vault.take_note("Solar powered bird feeder with a camera inside it", "idea", "")
    assert (tmp_path / "Ideas" / "2026-11-23 Solar powered bird feeder with a.md").exists()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("../../x", "x"),
        ("a:b/c", "a b c"),
        ('what? "really" <no>|*', "what really no"),
        ("CON", "CON"),  # the date prefix makes reserved names harmless
        ("  .hidden. ", "hidden"),
        ("x" * 300, "x" * 60),
        ("..", ""),
    ],
)
def test_safe_title(raw, expected):
    assert safe_title(raw) == expected


def test_nothing_is_written_outside_the_vault(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    vault = NoteVault(root, now=lambda: NOW)
    for title in ("../../escape", "..\\..\\escape", "/etc/escape", "C:\\escape", "C:escape"):
        vault.take_note("x", "idea", title)
    written = {p for p in tmp_path.rglob("*") if p.is_file()}
    assert written and all(root in p.parents for p in written)


@pytest.mark.parametrize("folder", ["..", "../outside", "..\\outside", "C:\\outside"])
def test_folders_outside_the_vault_are_refused(tmp_path, folder):
    root = tmp_path / "vault"
    root.mkdir()
    vault = NoteVault(root, daily_folder=folder, ideas_folder=folder, now=lambda: NOW)
    with pytest.raises(ToolError):
        vault.take_note("x", "note", "")
    with pytest.raises(ToolError):
        vault.take_note("x", "idea", "T")
    assert not [p for p in tmp_path.rglob("*") if p.is_file()]
    with pytest.raises(ToolError):
        vault.read_notes("2026-11-23")


def test_custom_folder_names(tmp_path):
    vault = NoteVault(tmp_path, daily_folder="Journal", ideas_folder="Box", now=lambda: NOW)
    vault.take_note("x", "note", "")
    vault.take_note("y", "idea", "T")
    assert (tmp_path / "Journal" / "2026-11-23.md").exists()
    assert (tmp_path / "Box" / "2026-11-23 T.md").exists()


def test_read_notes_returns_notes_and_ideas_for_the_day(vault):
    vault.take_note("Pick up dog food", "note", "")
    vault.take_note("A lamp that dims.", "idea", "Lamp")
    assert vault.read_notes("2026-11-23") == (
        "Notes for Monday 23 November 2026:\n"
        "- 16:38 Pick up dog food\n"
        "Ideas:\n"
        "- Lamp: A lamp that dims."
    )


def test_read_notes_empty_day(vault):
    assert vault.read_notes("2026-11-22") == "Nothing noted on Sunday 22 November 2026."


def test_read_notes_ignores_other_days(vault, tmp_path):
    vault.take_note("today", "note", "")
    assert "today" not in vault.read_notes("2026-11-24")


@pytest.mark.parametrize("bad", ["yesterday", "23/11/2026", "2026-13-01", "../x"])
def test_read_notes_bad_date(vault, bad):
    with pytest.raises(ToolError, match="2026-11-23"):
        vault.read_notes(bad)


def test_read_notes_caps_long_days(vault):
    for i in range(400):
        vault.take_note(f"note number {i} " + "x" * 40, "note", "")
    assert len(vault.read_notes("2026-11-23")) <= 4000
