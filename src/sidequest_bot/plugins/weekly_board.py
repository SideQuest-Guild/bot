"""Monday-morning weekly events board: one gold embed listing the week's scheduled events.

Every ``CHECK_INTERVAL_SECONDS``, for each guild with ``weekly_board_channel_id`` configured: once it's Monday at or
after 10:00 America/Chicago and this week hasn't been posted yet (``GuildConfig.weekly_board_last_week``), fetch
that week's scheduled events (Monday 00:00 to the following Monday 00:00 Chicago) via the bot-key
``weekly-board/events`` endpoint and post one embed, in time order, with a Discord timestamp per event so every
reader sees it in their own timezone. The week is then marked posted so a restart or a second tick that same
Monday never double-posts; because posting only ever happens while it's still Monday, a bot that was down past
10:00 catches up the moment it's back up that day, but never later in the week. An empty week posts nothing (the
week is still marked handled, so the bot doesn't keep checking it for the rest of the day).
"""

import logging
from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
from typing import TYPE_CHECKING, Final
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

from sidequest_bot.api_client import APIError, NotFoundError
from sidequest_bot.bot import SideQuestBot  # noqa: TC001  # discord.py evaluates app-command annotations at runtime
from sidequest_bot.lib.embeds import Colors, make_embed
from sidequest_common.schemas import EVENT_KIND_LABELS, GuildConfigUpdate, WeeklyBoardEvent
from sidequest_common.settings import WebSettings

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 300

GUILD_TZ: Final = ZoneInfo("America/Chicago")
"""US guild: the weekly board posts, and its week boundaries fall, on Central time."""

POST_TIME: Final = time(10, 0)
"""Local (Central) time of day the board posts."""


@lru_cache(maxsize=1)
def _web_settings() -> WebSettings:
    """``WebSettings`` (``WEB_*`` env vars), shared with the API: ``BASE_URL`` for event links."""
    return WebSettings()


def week_monday(now: datetime) -> date:
    """The Monday (Central date) of the week containing ``now``."""
    local = now.astimezone(GUILD_TZ)
    return local.date() - timedelta(days=local.weekday())


def week_window(monday: date) -> tuple[datetime, datetime]:
    """UTC ``(start, end)`` for ``monday`` 00:00 Central up to (not including) the following Monday 00:00 Central.

    Computed independently per bound via ``zoneinfo``, so a DST transition inside the week doesn't skew either end.
    """
    start = datetime.combine(monday, time.min, tzinfo=GUILD_TZ)
    end = datetime.combine(monday + timedelta(days=7), time.min, tzinfo=GUILD_TZ)
    return start.astimezone(UTC), end.astimezone(UTC)


def should_post(now: datetime, last_week: str | None) -> date | None:
    """The Monday to post the board for, or ``None`` if it isn't time yet or this week is already posted.

    Only ever returns a date on Monday at/after 10:00 Central: a bot that missed the window catches up the moment
    it's back up that same Monday, but a gap of more than a day never triggers a late post later in the week.
    """
    local = now.astimezone(GUILD_TZ)
    if local.weekday() != 0 or local.time() < POST_TIME:
        return None
    monday = local.date()
    if last_week == monday.isoformat():
        return None
    return monday


def _event_line(event: WeeklyBoardEvent, base_url: str, guild_id: int) -> str:
    weekday = event.starts_at.astimezone(GUILD_TZ).strftime("%A")
    kind_label = EVENT_KIND_LABELS[event.kind]
    url = f"{base_url.rstrip('/')}/events/detail/?id={event.id}"
    line = (
        f"**{weekday}** {discord.utils.format_dt(event.starts_at, style='F')} "
        f"({discord.utils.format_dt(event.starts_at, style='R')}) — {kind_label}: [{event.title}]({url})"
    )
    if event.discord_thread_id is not None:
        line += f" · [Thread](https://discord.com/channels/{guild_id}/{event.discord_thread_id})"
    return line


def build_board_embed(
    events: Sequence[WeeklyBoardEvent], monday: date, base_url: str, guild_id: int
) -> discord.Embed | None:
    """The board embed for ``monday``'s week, or ``None`` if ``events`` is empty (nothing is posted that week)."""
    if not events:
        return None
    sunday = monday + timedelta(days=6)
    title = f"This Week's Events: {monday:%b %-d} - {sunday:%b %-d}"
    description = "\n".join(_event_line(event, base_url, guild_id) for event in events)
    return make_embed(title, description, color=Colors.GOLD)


class WeeklyBoardCog(commands.Cog):
    """Monday-morning board poll plus the ``/weeklyboard channel``/``/weeklyboard preview`` commands."""

    weeklyboard_group = app_commands.Group(
        name="weeklyboard",
        description="Configure the weekly events board post.",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    def __init__(self, bot: SideQuestBot) -> None:
        self.bot = bot
        self._check_loop.start()

    async def cog_unload(self) -> None:
        """Stop the check loop when the cog is unloaded."""
        self._check_loop.cancel()

    @tasks.loop(seconds=CHECK_INTERVAL_SECONDS)
    async def _check_loop(self) -> None:
        for guild in list(self.bot.guilds):
            try:
                await self._check_guild(guild)
            except NotFoundError:
                # The weekly-board endpoints have no established guild-identity check to confirm this guild
                # really isn't the site guild, so a 404 here is just as likely a rolling deploy hitting an old
                # API pod that doesn't have the route yet. Never exclude; retry next tick.
                logger.info("Weekly board endpoint not available yet for guild %s; retrying next tick", guild.id)
            except APIError:
                # Must not escape: an unhandled exception stops a tasks.loop for good. Retried next tick.
                logger.warning("Weekly board check failed for guild %s", guild.id, exc_info=True)

    @_check_loop.before_loop
    async def _before_check_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def _check_guild(self, guild: discord.Guild) -> None:
        config = await self.bot.configs.get(guild.id)
        if config.weekly_board_channel_id is None:
            return
        monday = should_post(discord.utils.utcnow(), config.weekly_board_last_week)
        if monday is None:
            return
        channel = guild.get_channel(config.weekly_board_channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        await self._post_board(guild, channel, monday)

    async def _post_board(self, guild: discord.Guild, channel: discord.TextChannel, monday: date) -> None:
        start, end = week_window(monday)
        events = await self.bot.api.list_weekly_board_events(guild.id, start, end)
        embed = build_board_embed(events, monday, _web_settings().BASE_URL, guild.id)
        if embed is not None:
            try:
                await channel.send(embed=embed)
            except discord.HTTPException:
                logger.warning("Failed to post weekly board for guild %s", guild.id, exc_info=True)
                return
        await self.bot.configs.update(guild.id, GuildConfigUpdate(weekly_board_last_week=monday.isoformat()))

    @weeklyboard_group.command(name="channel", description="Set (or clear) the channel for the weekly events board.")
    @app_commands.describe(channel="Channel to post the weekly board in; omit to disable.")
    async def weeklyboard_channel(
        self, interaction: discord.Interaction[SideQuestBot], channel: discord.TextChannel | None = None
    ) -> None:
        """Update ``weekly_board_channel_id``."""
        guild = interaction.guild
        if guild is None:
            return
        await interaction.client.configs.update(
            guild.id, GuildConfigUpdate(weekly_board_channel_id=channel.id if channel else None)
        )
        message = (
            f"The weekly events board will post in {channel.mention} every Monday at 10:00 AM Central."
            if channel
            else "Weekly events board disabled."
        )
        await interaction.response.send_message(message, ephemeral=True)

    @weeklyboard_group.command(name="preview", description="Preview this week's events board without posting it.")
    async def weeklyboard_preview(self, interaction: discord.Interaction[SideQuestBot]) -> None:
        """Ephemeral preview of the current week's board, regardless of whether it's been posted yet."""
        guild = interaction.guild
        if guild is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        monday = week_monday(discord.utils.utcnow())
        start, end = week_window(monday)
        try:
            events = await interaction.client.api.list_weekly_board_events(guild.id, start, end)
        except APIError:
            logger.warning("Failed to fetch weekly board preview for guild %s", guild.id, exc_info=True)
            await interaction.followup.send("Couldn't reach the guild records; try again shortly.", ephemeral=True)
            return
        embed = build_board_embed(events, monday, _web_settings().BASE_URL, guild.id)
        if embed is None:
            await interaction.followup.send("No events scheduled this week.", ephemeral=True)
            return
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: SideQuestBot) -> None:
    """Register the cog."""
    await bot.add_cog(WeeklyBoardCog(bot))
