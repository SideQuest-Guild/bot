"""Monday-morning weekly events board: one card per scheduled event, each with sign-up buttons and live lists.

Every ``CHECK_INTERVAL_SECONDS``, for each guild with ``weekly_board_channel_id`` configured: once it's Monday at or
after 10:00 America/Chicago and this week hasn't been posted yet (``GuildConfig.weekly_board_last_week``), fetch
that week's scheduled events (Monday 00:00 to the following Monday 00:00 Chicago) via the bot-key
``weekly-board/events`` endpoint and post one card per event, in time order, under a "This Week's Events" heading
on the first. A card (``event_sync.build_event_card``) shows the event with Discord timestamps, so every reader sees
it in their own timezone, who's going, maybe or can't (as @-mentions), Going/Maybe/Can't buttons and "More info"/
"Thread" links. Each card's message is recorded with the API, so from then on the event sync redraws it whenever a
sign-up changes.

The week is then marked posted so a restart or a second tick that same Monday never double-posts; because posting
only ever happens while it's still Monday, a bot that was down past 10:00 catches up the moment it's back up that
day, but never later in the week. If the first card fails to post, nothing went out and the next tick retries; a
later card that fails is skipped, so a retry never repeats the cards already posted. An empty week posts nothing
(the week is still marked handled, so the bot doesn't keep checking it for the rest of the day).
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
from sidequest_bot.plugins.event_sync import card_from_board_event
from sidequest_common.schemas import GuildConfigUpdate, WeeklyBoardMessage
from sidequest_common.settings import WebSettings

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sidequest_common.schemas import WeeklyBoardEvent

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 300
MAX_PREVIEW_CARDS = 10
"""Discord's embeds-per-message limit, for ``/weeklyboard preview``'s one ephemeral message."""

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


def board_heading(monday: date) -> str:
    """The heading on the week's first card, e.g. "## This Week's Events: Nov 2 - Nov 8"."""
    sunday = monday + timedelta(days=6)
    return f"## This Week's Events: {monday:%b %-d} - {sunday:%b %-d}"


def build_board_cards(
    events: Sequence[WeeklyBoardEvent], guild: discord.Guild, base_url: str
) -> list[tuple[WeeklyBoardEvent, discord.Embed, discord.ui.View]]:
    """Each event with its card, in the order given (soonest first)."""
    return [(event, *card_from_board_event(event, guild=guild, base_url=base_url)) for event in events]


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
        for index, (event, embed, view) in enumerate(build_board_cards(events, guild, _web_settings().BASE_URL)):
            try:
                message = await channel.send(
                    content=board_heading(monday) if index == 0 else None,
                    embed=embed,
                    view=view,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                logger.warning("Failed to post a weekly board card for guild %s", guild.id, exc_info=True)
                if index == 0:
                    return
                continue
            await self._record_card(guild, event, message)
        await self.bot.configs.update(guild.id, GuildConfigUpdate(weekly_board_last_week=monday.isoformat()))

    async def _record_card(self, guild: discord.Guild, event: WeeklyBoardEvent, message: discord.Message) -> None:
        """Tell the API which message is ``event``'s card; if that fails, the card just stops updating."""
        try:
            await self.bot.api.set_weekly_board_message(
                guild.id, event.id, WeeklyBoardMessage(channel_id=message.channel.id, message_id=message.id)
            )
        except APIError:
            logger.warning("Failed to record the weekly board card for event %s", event.id, exc_info=True)

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
        cards = build_board_cards(events, guild, _web_settings().BASE_URL)
        if not cards:
            await interaction.followup.send("No events scheduled this week.", ephemeral=True)
            return
        content = board_heading(monday)
        if len(cards) > MAX_PREVIEW_CARDS:
            content += f"\n-# The first {MAX_PREVIEW_CARDS} of {len(cards)} cards; the board posts them all."
        await interaction.followup.send(
            content=content, embeds=[embed for _event, embed, _view in cards[:MAX_PREVIEW_CARDS]], ephemeral=True
        )


async def setup(bot: SideQuestBot) -> None:
    """Register the cog."""
    await bot.add_cog(WeeklyBoardCog(bot))
