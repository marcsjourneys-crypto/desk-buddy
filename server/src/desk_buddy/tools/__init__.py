"""Tools Claude can call. Register every tool in build_tools()."""

from __future__ import annotations

from pathlib import Path

from desk_buddy.config import Settings
from desk_buddy.tools.notes import NoteVault, note_tools
from desk_buddy.tools.registry import Tool, ToolRegistry


def build_tools(settings: Settings) -> ToolRegistry:
    """Every tool Buddy has, given the settings. Note tools need a vault path."""
    tools: list[Tool] = []
    if settings.obsidian_vault_path:
        vault = NoteVault(
            Path(settings.obsidian_vault_path),
            daily_folder=settings.obsidian_daily_folder,
            ideas_folder=settings.obsidian_ideas_folder,
        )
        tools += note_tools(vault)
    return ToolRegistry(tools)
