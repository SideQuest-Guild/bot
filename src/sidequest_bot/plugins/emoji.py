"""Server emoji: every role icon is also a custom emoji members can use in chat.

At startup, and when the bot joins a server, each PNG in ``sidequest_bot/assets/role_icons`` (the rank, class, gate
and badge icons) becomes a server emoji named after its file: ``first-steps.png`` is ``:first_steps:``,
``warrior.png`` is ``:warrior:``. A server emoji that already has the name is left alone, whoever made it. The bot
never renames, edits or deletes an emoji. If an admin deletes one of these, it comes back at the next restart; rename
it instead to keep the image under a different name.

Needs the Create Expressions (or Manage Expressions) permission. Without it, or once the server's static emoji slots
run out, the bot logs a warning and skips the rest.
"""

import logging
from functools import cache
from importlib.resources import files
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from sidequest_bot.bot import SideQuestBot  # noqa: TC001  # discord.py evaluates app-command annotations at runtime

if TYPE_CHECKING:
    from collections.abc import Iterable

logger = logging.getLogger(__name__)


def emoji_name(filename: str) -> str:
    """The emoji name for a role icon file: ``seasoned-adventurer.png`` -> ``seasoned_adventurer``."""
    return filename.removesuffix(".png").replace("-", "_")


@cache
def role_icon_emoji() -> dict[str, bytes]:
    """Every role icon's PNG, keyed by its emoji name, in name order."""
    icon_dir = files("sidequest_bot") / "assets" / "role_icons"
    icons = (entry for entry in icon_dir.iterdir() if entry.name.endswith(".png"))
    return {emoji_name(icon.name): icon.read_bytes() for icon in sorted(icons, key=lambda entry: entry.name)}


def plan_new_emoji(wanted: Iterable[str], existing: set[str], free_slots: int) -> tuple[list[str], list[str]]:
    """Split the wanted names the server lacks into ``(to create, left out for lack of slots)``.

    ``existing`` must already be lowercased: Discord treats ``:Warrior:`` and ``:warrior:`` as the same name.
    """
    missing = [name for name in wanted if name.lower() not in existing]
    room = max(free_slots, 0)
    return missing[:room], missing[room:]


class EmojiCog(commands.Cog):
    """Uploads the role icons as server emoji."""

    def __init__(self, bot: SideQuestBot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """Fill in any missing emoji on every server (``on_ready`` also fires after a reconnect; that's harmless)."""
        for guild in list(self.bot.guilds):
            await self.sync_guild(guild)

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        """A new server gets the emoji straight away."""
        await self.sync_guild(guild)

    @staticmethod
    async def sync_guild(guild: discord.Guild) -> list[str]:
        """Create the role icon emoji ``guild`` doesn't have yet; returns the names created."""
        permissions = guild.me.guild_permissions
        if not (permissions.create_expressions or permissions.manage_expressions):
            logger.warning("Can't add role icon emoji in guild %s: the bot lacks Create Expressions", guild.id)
            return []
        icons = role_icon_emoji()
        static_count = sum(1 for emoji in guild.emojis if not emoji.animated)
        to_create, no_room = plan_new_emoji(
            icons, {emoji.name.lower() for emoji in guild.emojis}, guild.emoji_limit - static_count
        )
        if no_room:
            logger.warning("Guild %s is out of emoji slots; skipped role icon emoji: %s", guild.id, ", ".join(no_room))
        created: list[str] = []
        for name in to_create:
            try:
                await guild.create_custom_emoji(name=name, image=icons[name], reason="Role icon emoji")
            except discord.HTTPException:
                logger.warning("Failed to add the %s emoji in guild %s", name, guild.id, exc_info=True)
                continue
            created.append(name)
        if created:
            logger.info("Added %d role icon emoji in guild %s", len(created), guild.id)
        return created


async def setup(bot: SideQuestBot) -> None:
    """Register the cog."""
    await bot.add_cog(EmojiCog(bot))
