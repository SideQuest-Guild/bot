"""Periodic roster sync and level milestone feed.

Keeps the website's character roster (``/api/guilds/{id}/characters/sync``) current with who's actually in the
guild, refreshing a handful of stale Battle.net profiles each tick; the same call auto-confirms members who own a
character on the in-game guild roster (``RosterSyncReport.members_confirmed``), which ``link_roles`` then reflects
as the roster role next tick. A changed result is logged at info level so the sync is visible in the logs without
spamming a channel.

After each sync, posts any level 40/60 milestones the website hasn't sent to Discord yet
(``/api/guilds/{id}/characters/milestones/pending``) to the guild's announcement channel
(``GuildConfig.announcement_channel_id``), then acks each one so it isn't posted twice.
"""

import logging
from http import HTTPStatus

import discord
from discord.ext import commands, tasks

from sidequest_bot.api_client import APIError, CharacterMilestone, NotFoundError, RosterSyncReport
from sidequest_bot.bot import SideQuestBot  # noqa: TC001  # discord.py evaluates app-command annotations at runtime
from sidequest_bot.lib.embeds import make_embed
from sidequest_bot.plugins.characters import CHARACTER_PAGE_URL, class_color

logger = logging.getLogger(__name__)

SYNC_INTERVAL_SECONDS = 1800


def build_milestone_embed(milestone: CharacterMilestone) -> discord.Embed:
    """Celebratory, class-colored embed for one level 40/60 milestone, linking to the character page."""
    embed = make_embed(
        title=f"{milestone.name} {milestone.surname} reached level {milestone.level}!",
        color=class_color(milestone.wow_class),
        timestamp=False,
    )
    embed.timestamp = milestone.reached_at
    embed.url = CHARACTER_PAGE_URL.format(slug=milestone.slug)
    return embed


def describe_sync(report: RosterSyncReport) -> str | None:
    """One-line summary of what changed in ``report``, or ``None`` if nothing did (an unchanged member count)."""
    if not (
        report.joined
        or report.left
        or report.profiles_refreshed
        or report.characters_linked
        or report.members_confirmed
    ):
        return None
    parts: list[str] = []
    if report.joined:
        parts.append(f"joined: {', '.join(report.joined)}")
    if report.left:
        parts.append(f"left: {', '.join(report.left)}")
    if report.profiles_refreshed:
        parts.append(f"{report.profiles_refreshed} profile(s) refreshed")
    if report.characters_linked:
        parts.append(f"{report.characters_linked} character(s) linked")
    if report.members_confirmed:
        parts.append(f"{report.members_confirmed} member(s) confirmed on the roster")
    return "; ".join(parts)


class RosterSyncCog(commands.Cog):
    """Periodic character roster sync with the website API."""

    def __init__(self, bot: SideQuestBot) -> None:
        self.bot = bot
        self._sync_loop.start()

    async def cog_unload(self) -> None:
        """Stop the sync loop when the cog is unloaded."""
        self._sync_loop.cancel()

    @tasks.loop(seconds=SYNC_INTERVAL_SECONDS)
    async def _sync_loop(self) -> None:
        for guild in list(self.bot.guilds):
            try:
                await self._sync_guild(guild)
            except NotFoundError:
                # The API and bot deploy separately; a 404 here is as likely a rolling deploy hitting an old API
                # pod that doesn't have the route yet as a genuinely non-site guild. Never exclude; retry next tick.
                logger.info("Roster sync endpoint not available yet for guild %s; retrying next tick", guild.id)
            except APIError as exc:
                if exc.status_code == HTTPStatus.SERVICE_UNAVAILABLE:
                    logger.info("Roster sync skipped for guild %s: Battle.net unconfigured or down", guild.id)
                else:
                    # Must not escape: an unhandled exception stops a tasks.loop for good. Retried next tick.
                    logger.warning("Roster sync failed for guild %s", guild.id, exc_info=True)

    @_sync_loop.before_loop
    async def _before_sync_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def _sync_guild(self, guild: discord.Guild) -> None:
        report = await self.bot.api.sync_roster(guild.id)
        if (summary := describe_sync(report)) is not None:
            logger.info("Roster sync for guild %s: %s", guild.id, summary)
        await self._post_milestones(guild)

    async def _post_milestones(self, guild: discord.Guild) -> None:
        try:
            milestones = await self.bot.api.list_pending_milestones(guild.id)
        except NotFoundError:
            logger.info("Milestones endpoint not available yet for guild %s; retrying next tick", guild.id)
            return
        except APIError:
            logger.warning("Fetching pending milestones failed for guild %s", guild.id, exc_info=True)
            return
        if not milestones:
            return
        config = await self.bot.configs.get(guild.id)
        if config.announcement_channel_id is None:
            logger.info(
                "Guild %s has %d pending level milestone(s) but no announcement channel set",
                guild.id,
                len(milestones),
            )
            return
        channel = guild.get_channel(config.announcement_channel_id)
        if not isinstance(channel, discord.TextChannel):
            logger.info("Guild %s's announcement channel isn't a text channel; milestones left pending", guild.id)
            return
        for milestone in milestones:
            await self._post_milestone(guild, channel, milestone)

    async def _post_milestone(
        self, guild: discord.Guild, channel: discord.TextChannel, milestone: CharacterMilestone
    ) -> None:
        try:
            message = await channel.send(embed=build_milestone_embed(milestone))
        except discord.HTTPException:
            logger.warning("Failed to post milestone %s for guild %s", milestone.id, guild.id, exc_info=True)
            return
        try:
            await self.bot.api.mark_milestone_posted(guild.id, milestone.id, message.id)
        except APIError:
            logger.warning("Failed to ack milestone %s for guild %s", milestone.id, guild.id, exc_info=True)


async def setup(bot: SideQuestBot) -> None:
    """Register the cog."""
    await bot.add_cog(RosterSyncCog(bot))
