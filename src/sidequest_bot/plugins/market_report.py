"""Daily market report: what to gather and what to craft for gold on the guild's auction house.

Every ``CHECK_INTERVAL_SECONDS``, for each guild with ``market_report_channel_id`` configured: once it's at or
after 10:00 America/Chicago and today (Central date) hasn't been posted yet (``GuildConfig.market_report_last_day``),
fetch the daily report via the bot-key ``market-report`` endpoint. No report yet, or one older than
``MARKET_STALE_HOURS``, skips this tick and retries the next one without marking the day posted. Otherwise the bot
posts one message with two embeds (gathering, then crafting) and a link view, then marks the day posted so a
restart or a second tick that same day never double-posts.

Gathering picks are grouped by profession (Herbalism, Mining, Skinning, Fishing, Cloth) with at most one pick per
level band (``1-24``, ``25-44``, ``45-60``), lowest band first, each band's best pick being the first one in it
(sections already rank best first). Crafting picks show each profession's top two. Both embeds trim picks to stay
under Discord's per-field (1024) and per-message (6000) character limits.
"""

import logging
from datetime import datetime, time
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Final
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

from sidequest_bot.api_client import APIError, NotFoundError
from sidequest_bot.bot import SideQuestBot  # noqa: TC001  # discord.py evaluates app-command annotations at runtime
from sidequest_bot.lib.embeds import Colors, link_button, link_view, make_embed
from sidequest_bot.plugins.auctions import format_price
from sidequest_bot.plugins.items import ITEM_PAGE_URL
from sidequest_common.schemas import GuildConfigUpdate
from sidequest_common.settings import WebSettings

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from sidequest_common.schemas import MarketCraftPick, MarketGatherPick, MarketReport

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 300

GUILD_TZ: Final = ZoneInfo("America/Chicago")
"""US guild: the market report posts on Central time."""

POST_TIME: Final = time(10, 0)
"""Local (Central) time of day the report posts."""

GATHER_PROFESSIONS: Final = ("Herbalism", "Mining", "Skinning", "Fishing", "Cloth")
LEVEL_BANDS: Final = ("1-24", "25-44", "45-60")
"""Level bands used for gathering picks, by a pick's ``min_level``."""
LEVEL_BAND_1_24_MAX = 24
LEVEL_BAND_25_44_MAX = 44
"""Highest ``min_level`` in each of the first two level bands; the rest fall in ``45-60``."""

MONEY_WHOLE_GOLD = 100 * 10000
"""From 100g, money shows whole gold."""
MONEY_GOLD = 10000
"""From 1g, money shows gold and silver."""
MONEY_WHOLE_SILVER = 10 * 100
"""From 10s, money shows whole silver; under that, silver and copper."""

MAX_CRAFT_PICKS = 2
"""Top crafting picks shown per profession."""

FIELD_VALUE_LIMIT = 1024
"""Discord's hard cap on one embed field's value."""

EMBED_TOTAL_LIMIT = 6000
"""Discord's hard cap on the combined length of every embed in one message."""


@lru_cache(maxsize=1)
def _web_settings() -> WebSettings:
    """``WebSettings`` (``WEB_*`` env vars), shared with the API: ``BASE_URL`` for report links."""
    return WebSettings()


def should_post(now: datetime, last_day: str | None) -> str | None:
    """Today's Central ISO date to post the report for, or ``None`` if it isn't time yet or today is already posted."""
    local = now.astimezone(GUILD_TZ)
    if local.time() < POST_TIME:
        return None
    today = local.date().isoformat()
    if last_day == today:
        return None
    return today


def band_for_level(min_level: int) -> str:
    """Which of :data:`LEVEL_BANDS` a gathering pick's ``min_level`` falls into."""
    if min_level <= LEVEL_BAND_1_24_MAX:
        return "1-24"
    if min_level <= LEVEL_BAND_25_44_MAX:
        return "25-44"
    return "45-60"


def band_picks(picks: Sequence[MarketGatherPick]) -> list[MarketGatherPick]:
    """At most one pick per level band, lowest band first; each band's pick is the first (best) one in it."""
    by_band: dict[str, MarketGatherPick] = {}
    for pick in picks:
        by_band.setdefault(band_for_level(pick.min_level), pick)
    return [by_band[band] for band in LEVEL_BANDS if band in by_band]


def format_change(change_pct: float | None) -> str | None:
    """``up 12%`` / ``down 8%`` / ``steady`` (under 1%), or ``None`` without enough history to compare."""
    if change_pct is None:
        return None
    rounded = round(change_pct)
    if rounded == 0:
        return "steady"
    direction = "up" if rounded > 0 else "down"
    return f"{direction} {abs(rounded)}%"


def format_money(copper: int) -> str:
    """``copper`` rounded to what a reader needs: ``128g``, ``4g 24s``, ``34s``, ``1s 90c``, ``33c``.

    Exact coppers on a 400g craft are noise; on a 2s herb they are the price.
    """
    copper = max(copper, 0)
    if copper >= MONEY_WHOLE_GOLD:
        return f"{round(copper / 10000):,}g"
    if copper >= MONEY_GOLD:
        silver = round(copper / 100)
        return format_price(silver * 100)
    if copper >= MONEY_WHOLE_SILVER:
        return f"{round(copper / 100)}s"
    return format_price(copper)


COOLDOWN_DAY_HOURS = 24
COOLDOWN_2DAY_HOURS = 48
COOLDOWN_4DAY_HOURS = 96


def format_sold_per_day(sold_per_day: float) -> str:
    """``round(sold_per_day)`` with a thousands separator, e.g. ``12,040``."""
    return f"{round(sold_per_day):,}"


def format_cooldown(cooldown_hours: float | None) -> str | None:
    """``Once a day.`` / ``Once every 2 days.`` / ``Once every 4 days.`` / ``Once every N hours.``, or ``None``."""
    if cooldown_hours is None:
        return None
    hours = round(cooldown_hours)
    if hours == COOLDOWN_DAY_HOURS:
        return "Once a day."
    if hours == COOLDOWN_2DAY_HOURS:
        return "Once every 2 days."
    if hours == COOLDOWN_4DAY_HOURS:
        return "Once every 4 days."
    return f"Once every {hours} hours."


def gather_pick_lines(pick: MarketGatherPick) -> tuple[str, str]:
    """A gathering pick's two lines: level, name, price and change; then zones, skill and sale rate."""
    change = format_change(pick.change_pct)
    change_suffix = f", {change}" if change else ""
    url = ITEM_PAGE_URL.format(item_id=pick.item.item_id)
    price_text = f"{format_money(pick.price)} each{change_suffix}"
    line1 = f"`Lvl {pick.min_level}-{pick.max_level}` **[{pick.item.name}]({url})** {price_text}"
    parts = [f"{', '.join(zone.name for zone in pick.zones)}."]
    if pick.skill:
        parts.append(f"Skill {pick.skill}.")
    parts.append(f"{format_sold_per_day(pick.sold_per_day)} sold a day.")
    return line1, " ".join(parts)


def craft_pick_lines(pick: MarketCraftPick) -> tuple[str, str]:
    """A crafting pick's two lines: name and profit a craft; then materials cost, sell price and sale rate."""
    url = ITEM_PAGE_URL.format(item_id=pick.item.item_id)
    sign = "+" if pick.profit >= 0 else "-"
    line1 = f"**[{pick.item.name}]({url})** {sign}{format_money(abs(pick.profit))} profit a craft"
    line2 = (
        f"Materials {format_money(pick.mats_cost)}. Sells for {format_money(pick.price)}. "
        f"{format_sold_per_day(pick.sold_per_day)} sold a day."
    )
    if (cooldown := format_cooldown(pick.cooldown_hours)) is not None:
        line2 = f"{line2} {cooldown}"
    return line1, line2


class _Section:
    """One profession's embed field in progress: its picks and how to render two lines per pick."""

    def __init__(self, profession: str, items: Sequence[Any], line_pair: Callable[[Any], tuple[str, str]]) -> None:
        self.profession = profession
        self.items = list(items)
        self._line_pair = line_pair

    def value(self) -> str | None:
        """The field's value (two lines per remaining item), or ``None`` if every item was trimmed away."""
        if not self.items:
            return None
        lines: list[str] = []
        for item in self.items:
            lines.extend(self._line_pair(item))
        return "\n".join(lines)


def _trim_to_field_limit(section: _Section) -> None:
    """Drop this section's last (lowest-priority) item until its value fits one embed field."""
    while section.items:
        value = section.value()
        if value is not None and len(value) <= FIELD_VALUE_LIMIT:
            return
        section.items = section.items[:-1]


def _total_length(title: str, description: str, sections: Sequence[_Section], footer: str) -> int:
    """Discord's embed-length accounting (title + description + field name/value pairs + footer), both embeds."""
    total = len(title) + len(description) + len(footer)
    for section in sections:
        value = section.value()
        if value is not None:
            total += len(section.profession) + len(value)
    return total


def _trim_to_total_limit(title: str, description: str, sections: list[_Section], footer: str) -> None:
    """Shrink sections, largest first, until the whole message fits Discord's combined embed limit."""
    for section in sections:
        _trim_to_field_limit(section)
    while _total_length(title, description, sections, footer) > EMBED_TOTAL_LIMIT:
        candidates = [section for section in sections if section.items]
        if not candidates:
            return
        max(candidates, key=lambda section: len(section.items)).items.pop()


def data_age(data_at: datetime, now: datetime) -> str:
    """How old the data is, such as ``Data from 1 hour ago.``."""
    hours = max(0, int((now - data_at).total_seconds() // 3600))
    if hours == 0:
        return "Data from the last hour."
    return f"Data from {hours} hour{'' if hours == 1 else 's'} ago."


def build_report_embeds(
    report: MarketReport, base_url: str, *, now: datetime | None = None
) -> tuple[discord.Embed, discord.Embed]:
    """The report's two embeds: gathering (with its link and intro) and crafting (with the footer and timestamp)."""
    now = now or discord.utils.utcnow()
    gather_by_profession = {section.profession: section for section in report.gathering}
    gather_sections = [
        _Section(profession, band_picks(gather_by_profession[profession].picks), gather_pick_lines)
        for profession in GATHER_PROFESSIONS
        if profession in gather_by_profession
    ]
    craft_sections = [
        _Section(section.profession, section.picks[:MAX_CRAFT_PICKS], craft_pick_lines) for section in report.crafting
    ]

    title = f"Market report for {now.astimezone(GUILD_TZ):%A, %B %-d}"
    description = "What sells on our auction house today. Prices are what items sold for in the last two weeks."
    footer = f"{report.house_name}. {data_age(report.data_at, now)}"
    _trim_to_total_limit(title, description, [*gather_sections, *craft_sections], footer)

    gather_embed = make_embed(title=title, description=description, color=Colors.SITE_GOLD, timestamp=False)
    gather_embed.url = f"{base_url}/market/"
    for section in gather_sections:
        if (value := section.value()) is not None:
            gather_embed.add_field(name=section.profession, value=value, inline=False)

    craft_embed = make_embed(title="Crafting", color=Colors.SITE_GOLD, timestamp=False)
    for section in craft_sections:
        if (value := section.value()) is not None:
            craft_embed.add_field(name=section.profession, value=value, inline=False)
    craft_embed.set_footer(text=footer)
    craft_embed.timestamp = report.data_at

    return gather_embed, craft_embed


def build_report_view(base_url: str) -> discord.ui.View:
    """A view holding the report's "Full report" and "This week" link buttons."""
    return link_view(
        link_button("Full report", f"{base_url}/market/"),
        link_button("This week", f"{base_url}/market/?view=week"),
    )


class MarketReportCog(commands.Cog):
    """Daily report poll plus the ``/marketreport channel``/``/marketreport preview`` commands."""

    marketreport_group = app_commands.Group(
        name="marketreport",
        description="Configure the daily market report post.",
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
                # The market-report endpoint has no established guild-identity check to confirm this guild really
                # isn't the site guild, so a 404 here is just as likely a rolling deploy hitting an old API pod
                # that doesn't have the route yet. Never exclude; retry next tick.
                logger.info("Market report endpoint not available yet for guild %s; retrying next tick", guild.id)
            except APIError:
                # Must not escape: an unhandled exception stops a tasks.loop for good. Retried next tick.
                logger.warning("Market report check failed for guild %s", guild.id, exc_info=True)

    @_check_loop.before_loop
    async def _before_check_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def _check_guild(self, guild: discord.Guild) -> None:
        config = await self.bot.configs.get(guild.id)
        if config.market_report_channel_id is None:
            return
        today = should_post(discord.utils.utcnow(), config.market_report_last_day)
        if today is None:
            return
        channel = guild.get_channel(config.market_report_channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        report = await self.bot.api.get_market_report(guild.id, kind="daily")
        if report is None or report.stale:
            # No feed yet, or the feed is too old to post: skip this tick and try again next time, without
            # marking today posted.
            return
        await self._post_report(guild, channel, report, today)

    async def _post_report(
        self, guild: discord.Guild, channel: discord.TextChannel, report: MarketReport, today: str
    ) -> None:
        base_url = _web_settings().BASE_URL
        gather_embed, craft_embed = build_report_embeds(report, base_url)
        try:
            await channel.send(
                embeds=[gather_embed, craft_embed],
                view=build_report_view(base_url),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            logger.warning("Failed to post the market report for guild %s", guild.id, exc_info=True)
            return
        await self.bot.configs.update(guild.id, GuildConfigUpdate(market_report_last_day=today))

    @marketreport_group.command(name="channel", description="Set (or clear) the channel for the daily market report.")
    @app_commands.describe(channel="Channel to post the market report in; omit to disable.")
    async def marketreport_channel(
        self, interaction: discord.Interaction[SideQuestBot], channel: discord.TextChannel | None = None
    ) -> None:
        """Update ``market_report_channel_id``."""
        guild = interaction.guild
        if guild is None:
            return
        await interaction.client.configs.update(
            guild.id, GuildConfigUpdate(market_report_channel_id=channel.id if channel else None)
        )
        message = (
            f"The daily market report will post in {channel.mention} every day at 10:00 AM Central."
            if channel
            else "Daily market report disabled."
        )
        await interaction.response.send_message(message, ephemeral=True)

    @marketreport_group.command(name="preview", description="Preview today's market report without posting it.")
    async def marketreport_preview(self, interaction: discord.Interaction[SideQuestBot]) -> None:
        """Ephemeral preview of the current daily report, regardless of whether it's been posted yet."""
        guild = interaction.guild
        if guild is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            report = await interaction.client.api.get_market_report(guild.id, kind="daily")
        except APIError:
            logger.warning("Failed to fetch market report preview for guild %s", guild.id, exc_info=True)
            await interaction.followup.send("Couldn't reach the guild records; try again shortly.", ephemeral=True)
            return
        if report is None:
            await interaction.followup.send("No market data yet.", ephemeral=True)
            return
        base_url = _web_settings().BASE_URL
        gather_embed, craft_embed = build_report_embeds(report, base_url)
        await interaction.followup.send(
            embeds=[gather_embed, craft_embed], view=build_report_view(base_url), ephemeral=True
        )


async def setup(bot: SideQuestBot) -> None:
    """Register the cog."""
    await bot.add_cog(MarketReportCog(bot))
