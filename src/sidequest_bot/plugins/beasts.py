"""Beast lookup: ``/beast <name>`` shows a tameable WoW: Forever beast's family and portrait, for hunters.

The website builds its whole tameable-beast catalog (``~800`` Battle.net media lookups) on the first ``/api/beasts``
call per API process, which can take 10-20 seconds — far past Discord's ~3 second autocomplete budget. Both
autocomplete callbacks below apply their own short timeout and fall back to no suggestions rather than erroring or
stalling; the ``/beast`` command itself, which has no such budget, waits out a slow first call normally.
"""

import asyncio
import logging
from http import HTTPStatus
from typing import TYPE_CHECKING, cast

import discord
from discord import app_commands
from discord.ext import commands

from sidequest_bot.api_client import APIError, NotFoundError
from sidequest_bot.bot import SideQuestBot  # noqa: TC001  # discord.py evaluates app-command annotations at runtime
from sidequest_bot.lib.embeds import Colors, link_button, link_view, make_embed, truncate

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sidequest_common.schemas import BeastSchema

logger = logging.getLogger(__name__)

BEAST_PAGE_URL = "https://sidequest-guild.com/beasts/"
BEAST_WOWHEAD_URL = "https://www.wowhead.com/forever/npc={creature_id}"
MAX_AUTOCOMPLETE_RESULTS = 25
MAX_OTHER_MATCHES = 5
AUTOCOMPLETE_TIMEOUT = 2.5
"""Seconds an autocomplete callback waits before giving up, safely inside Discord's ~3 second budget."""
NOT_FOUND_HINT = "Try a shorter word, or pick a suggestion as you type."
UNAVAILABLE_MESSAGE = "Beast lookup isn't available right now; try again shortly."


def autocomplete_label(beast: BeastSchema) -> str:
    """``name (family)``, or just the name if the beast has no family."""
    return f"{beast.name} ({beast.family})" if beast.family else beast.name


def match_option(match: BeastSchema) -> discord.SelectOption:
    """One search result as a select option, with its family as the description."""
    return discord.SelectOption(
        label=truncate(match.name, 100),
        value=str(match.id),
        description=truncate(match.family, 100) if match.family else None,
    )


def build_beast_embed(beast: BeastSchema) -> discord.Embed:
    """The beast's name, family and portrait (no per-beast site page exists to link as the embed's URL)."""
    embed = make_embed(title=beast.name, color=Colors.GREEN, timestamp=False)
    if beast.family:
        embed.add_field(name="Family", value=beast.family)
    embed.add_field(name="Tameable by hunters", value="Yes")
    if beast.image_url:
        embed.set_image(url=beast.image_url)
    return embed


def beast_link_buttons(creature_id: int) -> list[discord.ui.Button]:
    """Beasts page (the same URL for every beast; the page has no per-beast deep link) + Wowhead link buttons."""
    return [
        link_button("Beasts page", BEAST_PAGE_URL),
        link_button("Wowhead", BEAST_WOWHEAD_URL.format(creature_id=creature_id)),
    ]


def beast_link_view(creature_id: int) -> discord.ui.View:
    """A view holding just the beast's link buttons, for replies with no other matches to offer."""
    return link_view(*beast_link_buttons(creature_id))


async def _autocomplete_beast(
    interaction: discord.Interaction[SideQuestBot], current: str
) -> list[app_commands.Choice[str]]:
    """Up to 25 beasts matching ``current``; empty if the catalog isn't built within :data:`AUTOCOMPLETE_TIMEOUT`."""
    try:
        results = await asyncio.wait_for(
            interaction.client.api.search_beasts(current.strip() or None, limit=MAX_AUTOCOMPLETE_RESULTS),
            timeout=AUTOCOMPLETE_TIMEOUT,
        )
    except APIError, TimeoutError:
        return []
    return [app_commands.Choice(name=truncate(autocomplete_label(r), 100), value=str(r.id)) for r in results]


async def _autocomplete_beast_family(
    interaction: discord.Interaction[SideQuestBot], current: str
) -> list[app_commands.Choice[str]]:
    """Up to 25 beast families matching ``current``; empty if the catalog isn't built within the timeout."""
    try:
        families = await asyncio.wait_for(interaction.client.api.list_beast_families(), timeout=AUTOCOMPLETE_TIMEOUT)
    except APIError, TimeoutError:
        return []
    current_fold = current.strip().casefold()
    if current_fold:
        families = [f for f in families if current_fold in f.name.casefold()]
    return [
        app_commands.Choice(name=truncate(f"{f.name} ({f.count})", 100), value=str(f.id))
        for f in families[:MAX_AUTOCOMPLETE_RESULTS]
    ]


async def _fetch_beast_embed(interaction: discord.Interaction[SideQuestBot], creature_id: int) -> discord.Embed | str:
    """The beast's embed, or a plain-text error if the lookup failed."""
    try:
        beast = await interaction.client.api.get_beast(creature_id)
    except NotFoundError:
        return "That's not a tameable beast."
    except APIError as exc:
        if exc.status_code == HTTPStatus.SERVICE_UNAVAILABLE:
            return UNAVAILABLE_MESSAGE
        logger.warning("Beast lookup failed for %s", creature_id, exc_info=True)
        return "Couldn't look up that beast right now; try again shortly."
    return build_beast_embed(beast)


class _BeastMatchSelect(discord.ui.Select["BeastMatchView"]):
    """The select menu itself; its callback swaps the parent view's embed to the chosen beast."""

    def __init__(self, matches: Sequence[BeastSchema]) -> None:
        super().__init__(placeholder="Other matches", options=[match_option(match) for match in matches])

    async def callback(self, interaction: discord.Interaction[SideQuestBot]) -> None:
        creature_id = int(self.values[0])
        result = await _fetch_beast_embed(interaction, creature_id)
        if isinstance(result, str):
            await interaction.response.send_message(result, ephemeral=True)
            return
        view = cast("BeastMatchView", self.view)
        view.set_beast(creature_id)
        await interaction.response.edit_message(embed=result, view=view)


class BeastMatchView(discord.ui.View):
    """Link buttons for the shown beast plus a select menu of up to :data:`MAX_OTHER_MATCHES` other matches.

    Picking another match swaps the shown embed and repoints the Wowhead button at it (the Beasts page button
    never changes: the page has no per-beast deep link).
    """

    def __init__(self, creature_id: int, matches: Sequence[BeastSchema]) -> None:
        super().__init__(timeout=180)
        self._page_button, self._wowhead_button = beast_link_buttons(creature_id)
        self._select = _BeastMatchSelect(matches)
        self.add_item(self._select)
        self.add_item(self._page_button)
        self.add_item(self._wowhead_button)

    def set_beast(self, creature_id: int) -> None:
        """Repoint the Wowhead button at ``creature_id``."""
        self._wowhead_button.url = BEAST_WOWHEAD_URL.format(creature_id=creature_id)


class BeastsCog(commands.Cog):
    """``/beast`` lookup."""

    def __init__(self, bot: SideQuestBot) -> None:
        self.bot = bot

    @app_commands.command(name="beast", description="Look up a WoW: Forever tameable beast.")
    @app_commands.describe(name="Beast name, or pick a suggestion as you type", family="Only beasts in this family")
    @app_commands.autocomplete(name=_autocomplete_beast, family=_autocomplete_beast_family)
    async def beast_command(
        self, interaction: discord.Interaction[SideQuestBot], name: str, family: str | None = None
    ) -> None:
        """Resolve ``name`` to a creature id (from autocomplete, or a name search) and show its embed."""
        await interaction.response.defer()
        if name.isdigit():
            result = await _fetch_beast_embed(interaction, int(name))
            if isinstance(result, str):
                await interaction.followup.send(result, ephemeral=True)
                return
            await interaction.followup.send(embed=result, view=beast_link_view(int(name)))
            return
        family_id = int(family) if family is not None and family.isdigit() else None
        try:
            matches = await self.bot.api.search_beasts(name, family_id=family_id, limit=MAX_AUTOCOMPLETE_RESULTS)
        except APIError as exc:
            if exc.status_code == HTTPStatus.SERVICE_UNAVAILABLE:
                await interaction.followup.send(UNAVAILABLE_MESSAGE, ephemeral=True)
                return
            logger.warning("Beast search failed for %r", name, exc_info=True)
            await interaction.followup.send("Couldn't search beasts right now; try again shortly.", ephemeral=True)
            return
        if not matches:
            await interaction.followup.send(f"No beasts found matching **{name}**. {NOT_FOUND_HINT}", ephemeral=True)
            return
        best, others = matches[0], matches[1 : 1 + MAX_OTHER_MATCHES]
        result = await _fetch_beast_embed(interaction, best.id)
        if isinstance(result, str):
            await interaction.followup.send(result, ephemeral=True)
            return
        if others:
            await interaction.followup.send(embed=result, view=BeastMatchView(best.id, others))
        else:
            await interaction.followup.send(embed=result, view=beast_link_view(best.id))


async def setup(bot: SideQuestBot) -> None:
    """Register the cog."""
    await bot.add_cog(BeastsCog(bot))
