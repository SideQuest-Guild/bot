"""Periodic roster sync.

Keeps the website's character roster (``/api/guilds/{id}/characters/sync``) current with who's actually in the
guild, refreshing a handful of stale Battle.net profiles each tick.

Posts nothing to Discord; a later phase adds a level 40/60 feed. A changed result is logged at info level so the
sync is visible in the logs without spamming a channel.
"""

import logging
from http import HTTPStatus

import discord  # noqa: TC002  # discord.py evaluates app-command annotations at runtime
from discord.ext import commands, tasks

from sidequest_bot.api_client import APIError, NotFoundError, RosterSyncReport
from sidequest_bot.bot import SideQuestBot  # noqa: TC001  # discord.py evaluates app-command annotations at runtime

logger = logging.getLogger(__name__)

SYNC_INTERVAL_SECONDS = 1800


def describe_sync(report: RosterSyncReport) -> str | None:
    """One-line summary of what changed in ``report``, or ``None`` if nothing did (an unchanged member count)."""
    if not (report.joined or report.left or report.profiles_refreshed or report.characters_linked):
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


async def setup(bot: SideQuestBot) -> None:
    """Register the cog."""
    await bot.add_cog(RosterSyncCog(bot))
