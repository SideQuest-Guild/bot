"""Auction house prices: hourly sync + alert DMs, plus ``/price``, ``/watch``, ``/unwatch`` and ``/watches``.

Battle.net refreshes auction houses roughly hourly, between about :21 and :28 past the hour UTC
(see the auctions contract). The sync loop checks every few minutes and fires once per hour, at or after :35, so
it always runs after that window. After each sync it fetches any alerts the website queued and DMs each member,
acking every alert it attempted (including a closed-DM failure) so the website never resends it.

The money parser mirrors the addon's ``SideQuest.Market.ParsePrice``/``FormatPrice`` (``addon/SideQuest/Market.lua``):
free text like ``1g20s``, ``35s`` or ``80c``, or a plain decimal number of gold.
"""

import logging
import re
from http import HTTPStatus
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands, tasks

from sidequest_bot.api_client import (
    AUCTION_TREND_DAYS,
    MAX_AUCTION_WATCHES,
    APIError,
    AuctionAlertSchema,
    AuctionWatchCreate,
    ItemAuctionPrice,
    NotFoundError,
)
from sidequest_bot.bot import SideQuestBot  # noqa: TC001  # discord.py evaluates app-command annotations at runtime
from sidequest_bot.lib.embeds import Colors, make_embed, truncate
from sidequest_bot.plugins.items import ITEM_PAGE_URL, item_quality_color

if TYPE_CHECKING:
    from sidequest_bot.api_client import AuctionWatch, Item, ItemSummary

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 300
SYNC_MINUTE = 35
"""Battle.net updates houses ~:21-:28 past the hour UTC; sync once per hour at/after this minute."""

ITEM_SEARCH_MIN_LENGTH = 2
MAX_AUTOCOMPLETE_RESULTS = 25
UNAVAILABLE_MESSAGE = "Auction prices aren't available right now; try again shortly."
NOT_LISTED_MESSAGE = "Nobody is listing that item right now."

_PLAIN_GOLD_RE = re.compile(r"^\d+\.?\d*$")
_GOLD_RE = re.compile(r"(\d+)g")
_SILVER_RE = re.compile(r"(\d+)s")
_COPPER_RE = re.compile(r"(\d+)c")


def parse_price(text: str) -> int | None:
    """Copper from free text: ``1g20s``, ``35s``, ``80c``, or a plain decimal gold number like ``1.5``.

    ``None`` if ``text`` doesn't parse, or parses to zero. Mirrors ``SideQuest.Market.ParsePrice``.
    """
    stripped = re.sub(r"\s", "", text)
    if not stripped:
        return None
    if _PLAIN_GOLD_RE.match(stripped):
        gold = float(stripped)
        return round(gold * 10000)
    gold_match = _GOLD_RE.search(stripped)
    silver_match = _SILVER_RE.search(stripped)
    copper_match = _COPPER_RE.search(stripped)
    gold = int(gold_match[1]) if gold_match else 0
    silver = int(silver_match[1]) if silver_match else 0
    copper = int(copper_match[1]) if copper_match else 0
    if gold == 0 and silver == 0 and copper == 0:
        return None
    return gold * 10000 + silver * 100 + copper


def format_price(copper: int) -> str:
    """``copper`` as ``1g 20s 5c``, omitting zero parts (``0c`` when the whole amount is zero).

    Mirrors ``SideQuest.Market.FormatPrice``; distinct from :func:`sidequest_bot.plugins.items.format_sell_price`,
    which always shows all three parts.
    """
    gold, rest = divmod(copper, 10000)
    silver, bronze = divmod(rest, 100)
    parts: list[str] = []
    if gold:
        parts.append(f"{gold}g")
    if silver:
        parts.append(f"{silver}s")
    if bronze or not parts:
        parts.append(f"{bronze}c")
    return " ".join(parts)


def build_alert_embed(alert: AuctionAlertSchema) -> discord.Embed:
    """DM embed for one price alert: item name/link, price vs the member's max, quantity and house."""
    name = alert.item_name or f"Item {alert.item_id}"
    embed = make_embed(
        title=f"{name} is on sale",
        description=f"[View on the site]({ITEM_PAGE_URL.format(item_id=alert.item_id)})",
        color=Colors.GOLD,
    )
    if alert.icon_url:
        embed.set_thumbnail(url=alert.icon_url)
    embed.add_field(
        name="Price", value=f"{format_price(alert.unit_price)} (your max: {format_price(alert.max_unit_price)})"
    )
    embed.add_field(name="Quantity", value=str(alert.quantity))
    embed.add_field(name="House", value=alert.house_name)
    return embed


def build_price_embed(item: Item, price: ItemAuctionPrice) -> discord.Embed:
    """``/price``'s embed: lowest price and where, total listed, per-house lines, and the 14-day low/high."""
    embed = make_embed(title=item.name, color=item_quality_color(item.quality))
    embed.url = ITEM_PAGE_URL.format(item_id=item.item_id)
    if item.icon_url:
        embed.set_thumbnail(url=item.icon_url)
    if price.unit_price is None:
        embed.description = NOT_LISTED_MESSAGE
        return embed
    lowest = min(price.houses, key=lambda house: house.unit_price, default=None)
    where = f" at {lowest.house_name}" if lowest is not None else ""
    embed.add_field(name="Lowest price", value=f"{format_price(price.unit_price)}{where}", inline=False)
    embed.add_field(name="Total listed", value=str(price.quantity_total), inline=True)
    if price.trend:
        low = min(point.unit_price for point in price.trend)
        high = max(point.unit_price for point in price.trend)
        range_value = f"{format_price(low)} - {format_price(high)}"
        embed.add_field(name=f"{AUCTION_TREND_DAYS}-day range", value=range_value, inline=True)
    if price.houses:
        lines = "\n".join(
            f"{house.house_name}: {format_price(house.unit_price)} ({house.quantity})"
            for house in sorted(price.houses, key=lambda house: house.unit_price)
        )
        embed.add_field(name="By house", value=truncate(lines), inline=False)
    return embed


def item_choice(item: ItemSummary) -> app_commands.Choice[int]:
    """One item search result as an autocomplete choice, value is the item id."""
    return app_commands.Choice(name=truncate(f"{item.name} (ilvl {item.item_level})", 100), value=item.item_id)


async def _autocomplete_item(
    interaction: discord.Interaction[SideQuestBot], current: str
) -> list[app_commands.Choice[int]]:
    """Up to 25 items matching ``current`` (2+ chars); empty otherwise, since Battle.net needs a whole word."""
    if len(current) < ITEM_SEARCH_MIN_LENGTH:
        return []
    try:
        page = await interaction.client.api.search_items(current)
    except APIError:
        return []
    return [item_choice(item) for item in page.results[:MAX_AUTOCOMPLETE_RESULTS]]


def watch_line(watch: AuctionWatch) -> str:
    """One line of ``/watches``: item name, current price if known, and the member's max."""
    name = watch.item_name or f"Item {watch.item_id}"
    if watch.current_unit_price is not None:
        current = f"currently {format_price(watch.current_unit_price)}"
    else:
        current = "unlisted"
    return f"**{name}** — {current}, alert below {format_price(watch.max_unit_price)}"


class AuctionsCog(commands.Cog):
    """Hourly auction sync + alert DMs, plus the ``/price``, ``/watch``, ``/unwatch`` and ``/watches`` commands."""

    def __init__(self, bot: SideQuestBot) -> None:
        self.bot = bot
        self._synced_hour: dict[int, str] = {}
        self._sync_loop.start()

    async def cog_unload(self) -> None:
        """Stop the sync loop when the cog is unloaded."""
        self._sync_loop.cancel()

    @tasks.loop(seconds=CHECK_INTERVAL_SECONDS)
    async def _sync_loop(self) -> None:
        now = discord.utils.utcnow()
        if now.minute < SYNC_MINUTE:
            return
        hour_key = now.strftime("%Y-%m-%dT%H")
        for guild in list(self.bot.guilds):
            if self._synced_hour.get(guild.id) == hour_key:
                continue
            await self._sync_guild(guild)
            self._synced_hour[guild.id] = hour_key

    @_sync_loop.before_loop
    async def _before_sync_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def _sync_guild(self, guild: discord.Guild) -> None:
        try:
            await self.bot.api.sync_auctions(guild.id)
        except NotFoundError:
            # The API and bot deploy separately; a 404 here is as likely a rolling deploy hitting an old API pod
            # that doesn't have the route yet as a genuinely non-site guild. Never exclude; retry next hour.
            logger.info("Auction sync endpoint not available yet for guild %s; retrying next hour", guild.id)
            return
        except APIError as exc:
            if exc.status_code == HTTPStatus.SERVICE_UNAVAILABLE:
                logger.info("Auction sync skipped for guild %s: Battle.net unconfigured or down", guild.id)
            else:
                # Must not escape: an unhandled exception stops a tasks.loop for good. Retried next hour.
                logger.warning("Auction sync failed for guild %s", guild.id, exc_info=True)
            return
        await self._send_pending_alerts(guild)

    async def _send_pending_alerts(self, guild: discord.Guild) -> None:
        try:
            alerts = await self.bot.api.pending_auction_alerts(guild.id)
        except NotFoundError:
            logger.info("Auction alerts endpoint not available yet for guild %s", guild.id)
            return
        except APIError:
            logger.warning("Fetching pending auction alerts failed for guild %s", guild.id, exc_info=True)
            return
        for alert in alerts:
            await self._send_alert(guild, alert)

    async def _send_alert(self, guild: discord.Guild, alert: AuctionAlertSchema) -> None:
        user = await self._resolve_user(guild, alert.discord_user_id)
        if user is None:
            logger.info("Could not find Discord user %s for auction alert %s", alert.discord_user_id, alert.id)
        else:
            try:
                await user.send(embed=build_alert_embed(alert))
            except discord.Forbidden:
                logger.info("Could not DM auction alert to %s: DMs are closed", alert.discord_user_id)
            except discord.HTTPException:
                logger.warning("Failed to DM auction alert to %s", alert.discord_user_id, exc_info=True)
        try:
            await self.bot.api.ack_auction_alert(guild.id, alert.id)
        except APIError:
            logger.warning("Failed to ack auction alert %s for guild %s", alert.id, guild.id, exc_info=True)

    async def _resolve_user(self, guild: discord.Guild, discord_user_id: int) -> discord.User | discord.Member | None:
        if (member := guild.get_member(discord_user_id)) is not None:
            return member
        if (user := self.bot.get_user(discord_user_id)) is not None:
            return user
        try:
            return await self.bot.fetch_user(discord_user_id)
        except discord.NotFound:
            return None
        except discord.HTTPException:
            logger.warning("Could not fetch Discord user %s for auction alert", discord_user_id, exc_info=True)
            return None

    # --- Commands

    @app_commands.command(name="price", description="Look up WoW: Forever auction prices for an item.")
    @app_commands.describe(item="Item name (whole words only, e.g. bless or thunderfury)")
    @app_commands.autocomplete(item=_autocomplete_item)
    async def price_command(self, interaction: discord.Interaction[SideQuestBot], item: int) -> None:
        """Lowest price, total listed, per-house breakdown and 14-day range for ``item``."""
        await interaction.response.defer(ephemeral=True)
        try:
            item_data = await self.bot.api.get_item(item)
            price = await self.bot.api.get_item_price(item)
        except NotFoundError:
            await interaction.followup.send("That item is gone.", ephemeral=True)
            return
        except APIError as exc:
            if exc.status_code == HTTPStatus.SERVICE_UNAVAILABLE:
                await interaction.followup.send(UNAVAILABLE_MESSAGE, ephemeral=True)
                return
            logger.warning("Price lookup failed for item %s", item, exc_info=True)
            await interaction.followup.send("Couldn't look up prices right now; try again shortly.", ephemeral=True)
            return
        await interaction.followup.send(embed=build_price_embed(item_data, price), ephemeral=True)

    @app_commands.command(name="watch", description="Get a DM when an item's auction price drops to your target.")
    @app_commands.describe(item="Item name", price="Alert at or below this price, e.g. 1g20s, 35s, 80c")
    @app_commands.autocomplete(item=_autocomplete_item)
    @app_commands.guild_only()
    async def watch_command(self, interaction: discord.Interaction[SideQuestBot], item: int, price: str) -> None:
        """Add or replace a watch on ``item`` at ``price``; up to :data:`MAX_AUCTION_WATCHES` per member."""
        if interaction.guild_id is None:
            return
        copper = parse_price(price)
        if copper is None:
            await interaction.response.send_message(
                "Couldn't understand that price. Try something like 1g20s, 35s or 80c.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        try:
            watch = await self.bot.api.upsert_auction_watch(
                interaction.guild_id, interaction.user.id, AuctionWatchCreate(item_id=item, max_unit_price=copper)
            )
        except NotFoundError as exc:
            await interaction.followup.send(exc.detail, ephemeral=True)
            return
        except APIError as exc:
            if exc.status_code == HTTPStatus.CONFLICT:
                await interaction.followup.send(
                    f"You can only watch {MAX_AUCTION_WATCHES} items; remove one with /unwatch first.",
                    ephemeral=True,
                )
                return
            if exc.status_code == HTTPStatus.SERVICE_UNAVAILABLE:
                await interaction.followup.send(UNAVAILABLE_MESSAGE, ephemeral=True)
                return
            logger.warning("Setting auction watch failed for item %s", item, exc_info=True)
            await interaction.followup.send("Couldn't save that watch right now; try again shortly.", ephemeral=True)
            return
        name = watch.item_name or f"Item {watch.item_id}"
        await interaction.followup.send(
            f"Watching **{name}** — you'll be DMed when it drops to {format_price(watch.max_unit_price)} or below.",
            ephemeral=True,
        )

    @app_commands.command(name="unwatch", description="Stop watching an item's auction price.")
    @app_commands.describe(item="Item name")
    @app_commands.autocomplete(item=_autocomplete_item)
    @app_commands.guild_only()
    async def unwatch_command(self, interaction: discord.Interaction[SideQuestBot], item: int) -> None:
        """Remove the member's watch on ``item``."""
        if interaction.guild_id is None:
            return
        await interaction.response.defer(ephemeral=True)
        try:
            await self.bot.api.delete_auction_watch(interaction.guild_id, interaction.user.id, item)
        except NotFoundError as exc:
            await interaction.followup.send(exc.detail, ephemeral=True)
            return
        except APIError as exc:
            if exc.status_code == HTTPStatus.SERVICE_UNAVAILABLE:
                await interaction.followup.send(UNAVAILABLE_MESSAGE, ephemeral=True)
                return
            logger.warning("Removing auction watch failed for item %s", item, exc_info=True)
            await interaction.followup.send("Couldn't remove that watch right now; try again shortly.", ephemeral=True)
            return
        await interaction.followup.send("Watch removed.", ephemeral=True)

    @app_commands.command(name="watches", description="List your auction price watches.")
    @app_commands.guild_only()
    async def watches_command(self, interaction: discord.Interaction[SideQuestBot]) -> None:
        """Every item the member is watching, with its current price and alert threshold."""
        if interaction.guild_id is None:
            return
        await interaction.response.defer(ephemeral=True)
        try:
            watches = await self.bot.api.list_auction_watches(interaction.guild_id, interaction.user.id)
        except NotFoundError as exc:
            await interaction.followup.send(exc.detail, ephemeral=True)
            return
        except APIError as exc:
            if exc.status_code == HTTPStatus.SERVICE_UNAVAILABLE:
                await interaction.followup.send(UNAVAILABLE_MESSAGE, ephemeral=True)
                return
            logger.warning("Listing auction watches failed", exc_info=True)
            await interaction.followup.send("Couldn't load your watches right now; try again shortly.", ephemeral=True)
            return
        if not watches:
            await interaction.followup.send("You aren't watching any items. Use /watch to add one.", ephemeral=True)
            return
        embed = make_embed(title="Your auction watches", color=Colors.GOLD)
        embed.description = truncate("\n".join(watch_line(watch) for watch in watches), 4096)
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: SideQuestBot) -> None:
    """Register the cog."""
    await bot.add_cog(AuctionsCog(bot))
