"""Item lookup: ``/item <name>`` and chat links to an item's site page both show the same tooltip embed.

Battle.net only matches whole words after English stemming (``bless`` finds Blessed, ``thund`` finds nothing), so
search happens on submit rather than as-you-type, and a failed search says so. The item schemas aren't in the
``sidequest-common`` pin yet, so :mod:`sidequest_bot.api_client` defines them locally.
"""

import logging
import re
from http import HTTPStatus
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from sidequest_bot.api_client import APIError, Item, ItemSummary, NotFoundError
from sidequest_bot.bot import SideQuestBot  # noqa: TC001  # discord.py evaluates app-command annotations at runtime
from sidequest_bot.lib.embeds import Colors, make_embed, truncate

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

ITEM_PAGE_URL = "https://sidequest-guild.com/items/detail/?id={item_id}"
WOWHEAD_URL = "https://www.wowhead.com/forever/item={item_id}"
ITEM_LINK_RE = re.compile(r"https://sidequest-guild\.com/items/detail/\?id=(\d+)")
MAX_LINKS_PER_MESSAGE = 3
"""Caps how many item links in one message get unfurled, so a pasted wall of links can't spam the channel."""
MAX_OTHER_MATCHES = 5
DESCRIPTION_LIMIT = 4096
NOT_FOUND_HINT = "Use a whole word, like bless or thunderfury."
UNAVAILABLE_MESSAGE = "Item lookup isn't available right now; try again shortly."

QUALITY_COLORS: dict[str, discord.Colour] = {
    "POOR": discord.Colour(0x9D9D9D),
    "COMMON": discord.Colour(0xFFFFFF),
    "UNCOMMON": discord.Colour(0x1EFF00),
    "RARE": discord.Colour(0x0070DD),
    "EPIC": discord.Colour(0xA335EE),
    "LEGENDARY": discord.Colour(0xFF8000),
}


def item_quality_color(quality: str) -> discord.Colour:
    """The quality's WoW color, or the house slate for a quality with no standard loot color (artifact, heirloom)."""
    return QUALITY_COLORS.get(quality, Colors.SLATE)


def format_sell_price(copper: int) -> str:
    """``copper`` as ``Ng Ss Cc``, matching how the website shows vendor prices."""
    gold, rest = divmod(copper, 10000)
    silver, bronze = divmod(rest, 100)
    return f"{gold}g {silver}s {bronze}c"


def build_item_embed(item: Item) -> discord.Embed:
    """The item's tooltip as an embed: quality color, icon, tooltip lines, sell price, and links to both sites."""
    lines = [*item.tooltip, "", f"Sells for {format_sell_price(item.sell_price)}."]
    embed = make_embed(
        title=item.name,
        description=truncate("\n".join(lines), DESCRIPTION_LIMIT),
        color=item_quality_color(item.quality),
        timestamp=False,
    )
    if item.icon_url:
        embed.set_thumbnail(url=item.icon_url)
    embed.add_field(
        name="Links",
        value=f"[Item page]({ITEM_PAGE_URL.format(item_id=item.item_id)}) • "
        f"[Wowhead]({WOWHEAD_URL.format(item_id=item.item_id)})",
        inline=False,
    )
    return embed


def match_option(match: ItemSummary) -> discord.SelectOption:
    """One search result as a select option, with quality/slot/ilvl as its description."""
    return discord.SelectOption(
        label=truncate(match.name, 100),
        value=str(match.item_id),
        description=truncate(f"{match.quality_name} • {match.inventory_type_name} • ilvl {match.item_level}", 100),
    )


async def _fetch_item_embed(interaction: discord.Interaction[SideQuestBot], item_id: int) -> discord.Embed | str:
    """The item's embed, or a plain-text error if the lookup failed."""
    try:
        item = await interaction.client.api.get_item(item_id)
    except NotFoundError:
        return "That item is gone."
    except APIError as exc:
        if exc.status_code == HTTPStatus.SERVICE_UNAVAILABLE:
            return UNAVAILABLE_MESSAGE
        logger.warning("Item lookup failed for %s", item_id, exc_info=True)
        return "Couldn't look up that item right now; try again shortly."
    return build_item_embed(item)


class _ItemMatchSelect(discord.ui.Select["ItemMatchView"]):
    """The select menu itself; its callback swaps the parent view's embed to the chosen item."""

    def __init__(self, matches: Sequence[ItemSummary]) -> None:
        super().__init__(placeholder="Other matches", options=[match_option(match) for match in matches])

    async def callback(self, interaction: discord.Interaction[SideQuestBot]) -> None:
        item_id = int(self.values[0])
        result = await _fetch_item_embed(interaction, item_id)
        if isinstance(result, str):
            await interaction.response.send_message(result, ephemeral=True)
            return
        await interaction.response.edit_message(embed=result, view=self.view)


class ItemMatchView(discord.ui.View):
    """A select menu of up to :data:`MAX_OTHER_MATCHES` other search matches, swapping the shown embed on pick."""

    def __init__(self, matches: Sequence[ItemSummary]) -> None:
        super().__init__(timeout=180)
        self._select = _ItemMatchSelect(matches)
        self.add_item(self._select)


class ItemsCog(commands.Cog):
    """``/item`` lookup plus unfurling item links pasted in chat."""

    def __init__(self, bot: SideQuestBot) -> None:
        self.bot = bot

    @app_commands.command(name="item", description="Look up a WoW: Forever item.")
    @app_commands.describe(name="Item name (whole words only, e.g. bless or thunderfury)")
    async def item_command(self, interaction: discord.Interaction[SideQuestBot], name: str) -> None:
        """Search, then show the best match's tooltip; up to 5 other matches are offered in a select menu."""
        await interaction.response.defer()
        try:
            page = await self.bot.api.search_items(name)
        except APIError as exc:
            if exc.status_code == HTTPStatus.SERVICE_UNAVAILABLE:
                await interaction.followup.send(UNAVAILABLE_MESSAGE, ephemeral=True)
                return
            logger.warning("Item search failed for %r", name, exc_info=True)
            await interaction.followup.send("Couldn't search items right now; try again shortly.", ephemeral=True)
            return
        if not page.results:
            await interaction.followup.send(f"No items found for **{name}**. {NOT_FOUND_HINT}", ephemeral=True)
            return
        best, others = page.results[0], page.results[1 : 1 + MAX_OTHER_MATCHES]
        result = await _fetch_item_embed(interaction, best.item_id)
        if isinstance(result, str):
            await interaction.followup.send(result, ephemeral=True)
            return
        if others:
            await interaction.followup.send(embed=result, view=ItemMatchView(others))
        else:
            await interaction.followup.send(embed=result)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Unfurl up to 3 item-detail links per message into their tooltip embed."""
        if message.author.bot:
            return
        seen: list[int] = []
        for match in ITEM_LINK_RE.finditer(message.content):
            item_id = int(match[1])
            if item_id not in seen:
                seen.append(item_id)
            if len(seen) >= MAX_LINKS_PER_MESSAGE:
                break
        for item_id in seen:
            try:
                item = await self.bot.api.get_item(item_id)
            except NotFoundError:
                continue
            except APIError:
                logger.warning("Item link unfurl failed for %s", item_id, exc_info=True)
                continue
            try:
                await message.channel.send(embed=build_item_embed(item))
            except discord.HTTPException:
                logger.warning("Failed to post item unfurl for %s in channel %s", item_id, message.channel.id)


async def setup(bot: SideQuestBot) -> None:
    """Register the cog."""
    await bot.add_cog(ItemsCog(bot))
