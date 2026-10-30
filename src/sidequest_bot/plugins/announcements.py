"""Guild hub announcements: posts officer announcements queued for Discord (game `discord: true` or the site).

Every ``CHECK_INTERVAL_SECONDS``, for each guild with ``announcement_channel_id`` configured:
``GET /api/guilds/{guild_id}/announcements/pending`` lists announcements not yet posted, each posted as one embed
(title "Announcement", author name = the announcement's author, description = its text), then acked via
``POST /api/guilds/{guild_id}/announcements/{id}/posted`` so it isn't posted again. No channel configured means
the guild is skipped quietly (not even polled) rather than erroring.
"""

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands, tasks

from sidequest_bot.api_client import APIError, NotFoundError
from sidequest_bot.bot import SideQuestBot  # noqa: TC001  # discord.py evaluates app-command annotations at runtime
from sidequest_bot.lib.embeds import Colors, make_embed
from sidequest_common.schemas import GuildConfigUpdate

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sidequest_common.schemas import PendingAnnouncement

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 120

ANNOUNCEMENT_TITLE = "Announcement"


def build_announcement_embed(announcement: PendingAnnouncement) -> discord.Embed:
    """One gold embed for an announcement: title "Announcement", author name, text as the description."""
    embed = make_embed(ANNOUNCEMENT_TITLE, announcement.text, color=Colors.GOLD)
    embed.set_author(name=announcement.author)
    return embed


class AnnouncementsCog(commands.Cog):
    """Periodic pending-announcement poll: posts to ``announcement_channel_id``, then acks."""

    announcements_group = app_commands.Group(
        name="announcements",
        description="Configure the channel for guild announcements.",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    def __init__(self, bot: SideQuestBot) -> None:
        self.bot = bot
        self._check_loop.start()

    async def cog_unload(self) -> None:
        """Stop the check loop when the cog is unloaded."""
        self._check_loop.cancel()

    @announcements_group.command(name="channel", description="Set (or clear) the channel guild announcements post in.")
    @app_commands.describe(channel="Channel to post announcements in; omit to disable.")
    async def announcements_channel(
        self, interaction: discord.Interaction[SideQuestBot], channel: discord.TextChannel | None = None
    ) -> None:
        """Update ``announcement_channel_id``."""
        guild = interaction.guild
        if guild is None:
            return
        await interaction.client.configs.update(
            guild.id, GuildConfigUpdate(announcement_channel_id=channel.id if channel else None)
        )
        message = f"Guild announcements will post in {channel.mention}." if channel else "Guild announcements disabled."
        await interaction.response.send_message(message, ephemeral=True)

    @tasks.loop(seconds=CHECK_INTERVAL_SECONDS)
    async def _check_loop(self) -> None:
        for guild in list(self.bot.guilds):
            try:
                await self._check_guild(guild)
            except NotFoundError:
                # The announcements endpoints have no established guild-identity check to confirm this guild
                # really isn't the site guild, so a 404 here is just as likely a rolling deploy hitting an old
                # API pod that doesn't have the route yet. Never exclude; retry next tick.
                logger.info("Announcements endpoint not available yet for guild %s; retrying next tick", guild.id)
            except APIError:
                # Must not escape: an unhandled exception stops a tasks.loop for good. Retried next tick.
                logger.warning("Announcement check failed for guild %s", guild.id, exc_info=True)

    @_check_loop.before_loop
    async def _before_check_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def _check_guild(self, guild: discord.Guild) -> None:
        config = await self.bot.configs.get(guild.id)
        if config.announcement_channel_id is None:
            return
        channel = guild.get_channel(config.announcement_channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        pending = await self.bot.api.list_pending_announcements(guild.id)
        await self._post_pending(guild, channel, pending)

    async def _post_pending(
        self, guild: discord.Guild, channel: discord.TextChannel, pending: Sequence[PendingAnnouncement]
    ) -> None:
        for announcement in pending:
            await self._post_one(guild, channel, announcement)

    async def _post_one(
        self, guild: discord.Guild, channel: discord.TextChannel, announcement: PendingAnnouncement
    ) -> None:
        embed = build_announcement_embed(announcement)
        try:
            message = await channel.send(embed=embed)
        except discord.HTTPException:
            logger.warning("Failed to post announcement %s for guild %s", announcement.id, guild.id, exc_info=True)
            return
        try:
            await self.bot.api.mark_announcement_posted(guild.id, announcement.id, message.id)
        except APIError:
            logger.warning("Failed to ack announcement %s for guild %s", announcement.id, guild.id, exc_info=True)


async def setup(bot: SideQuestBot) -> None:
    """Register the cog."""
    await bot.add_cog(AnnouncementsCog(bot))
