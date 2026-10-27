"""Shared embed styling so every plugin's output looks like one bot."""

import discord


class Colors:
    """Palette, matching the guild's rank role colors."""

    GOLD = discord.Colour(0xF1C40F)  # Quest Master / level-ups / highlights
    PURPLE = discord.Colour(0x9B59B6)  # Quest Guide / moderation
    ORANGE = discord.Colour(0xE67E22)  # warnings
    BLUE = discord.Colour(0x3498DB)  # info
    GREEN = discord.Colour(0x2ECC71)  # success / joins / role added
    RED = discord.Colour(0xE74C3C)  # destructive: deletes, bans, leaves
    SLATE = discord.Colour(0x607D8B)  # neutral / edits


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
