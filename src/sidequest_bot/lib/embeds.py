"""Shared embed styling so every plugin's output looks like one bot."""

from typing import TYPE_CHECKING, Final

import discord

if TYPE_CHECKING:
    from collections.abc import Sequence

DESCRIPTION_LIMIT: Final = 4096
"""Discord's hard cap on an embed's ``description``."""


class Colors:
    """Palette, matching the guild's rank role colors."""

    GOLD = discord.Colour(0xF1C40F)  # Quest Master / level-ups / highlights
    PURPLE = discord.Colour(0x9B59B6)  # Quest Guide / moderation
    ORANGE = discord.Colour(0xE67E22)  # warnings
    BLUE = discord.Colour(0x3498DB)  # info
    GREEN = discord.Colour(0x2ECC71)  # success / joins / role added
    RED = discord.Colour(0xE74C3C)  # destructive: deletes, bans, leaves
    SLATE = discord.Colour(0x607D8B)  # neutral / edits
    SITE_GOLD = discord.Colour(0xD8AB5C)  # sidequest-guild.com's gold: public panels such as role menus
    BLIZZARD_BLUE = discord.Colour(0x148EFF)  # Blizzard blue posts in the WoW: Forever feed
    WOWHEAD_RED = discord.Colour(0xC41F1F)  # Wowhead articles in the WoW: Forever feed


def pack_lines(lines: Sequence[str], limit: int = DESCRIPTION_LIMIT) -> list[str]:
    """Greedily pack ``lines`` into newline-joined chunks no longer than ``limit`` characters."""
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in lines:
        added = len(line) + (1 if current else 0)
        if current and current_len + added > limit:
            chunks.append("\n".join(current))
            current = [line]
            current_len = len(line)
        else:
            current.append(line)
            current_len += added
    if current:
        chunks.append("\n".join(current))
    return chunks or [""]


def make_embed(
    title: str | None = None,
    description: str | None = None,
    *,
    color: discord.Colour = Colors.BLUE,
    timestamp: bool = True,
) -> discord.Embed:
    """Embed with the house color and, by default, a timestamp."""
    return discord.Embed(
        title=title,
        description=description,
        colour=color,
        timestamp=discord.utils.utcnow() if timestamp else None,
    )


def user_line(user: discord.Member | discord.User) -> str:
    """``@mention (name, ID 123)``; renders even if the user has left."""
    return f"{user.mention} (`{user}`, ID `{user.id}`)"


def truncate(text: str, limit: int = 1024) -> str:
    """Trim ``text`` to fit an embed field (default 1024 chars)."""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def link_button(label: str, url: str) -> discord.ui.Button:
    """A link-style button; needs no callback, so it works in views with any timeout, including none."""
    return discord.ui.Button(style=discord.ButtonStyle.link, label=label, url=url)


def link_view(*buttons: discord.ui.Button, timeout: float | None = None) -> discord.ui.View:
    """A view holding only link buttons, in order."""
    view = discord.ui.View(timeout=timeout)
    for button in buttons:
        view.add_item(button)
    return view
