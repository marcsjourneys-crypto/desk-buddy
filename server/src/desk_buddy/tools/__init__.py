"""Tools Claude can call. Register every tool in build_tools()."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from desk_buddy.config import Settings
from desk_buddy.tools.notes import NoteVault, note_tools
from desk_buddy.tools.registry import Tool, ToolRegistry

if TYPE_CHECKING:
    from desk_buddy.tools.launch import LaunchTools
    from desk_buddy.tools.spotify import MusicTools


def build_tools(
    settings: Settings,
    *,
    music: MusicTools | None = None,
    launch: LaunchTools | None = None,
) -> ToolRegistry:
    """Every tool CARL has, given the settings. Note tools need a vault path; music tools
    are passed in only when computer control is on and Spotify is logged in; the app and
    website tools only when computer control is on (each only if its list isn't empty)."""
    tools: list[Tool] = []
    if settings.obsidian_vault_path:
        vault = NoteVault(
            Path(settings.obsidian_vault_path),
            daily_folder=settings.obsidian_daily_folder,
            ideas_folder=settings.obsidian_ideas_folder,
        )
        tools += note_tools(vault)
    if music is not None:
        tools += music.tools()
    if launch is not None:
        tools += launch.tools()
    return ToolRegistry(tools)
