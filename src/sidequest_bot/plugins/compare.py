"""Character comparison: ``/compare <character1> <character2>`` is ``/who`` for two characters side by side.

Reuses ``/who``'s slug parsing, autocomplete and ``get_character`` lookup (run concurrently for both characters), so
the same roster/Battle.net rules and error handling apply to each side.
"""

import asyncio
import logging
from http import HTTPStatus
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from sidequest_bot.api_client import APIError, CharacterPage, NotFoundError
from sidequest_bot.bot import SideQuestBot  # noqa: TC001  # discord.py evaluates app-command annotations at runtime
from sidequest_bot.lib.embeds import Colors, make_embed, truncate
from sidequest_bot.plugins.characters import (
    NAME_HINT,
    _autocomplete_character,
    build_description,
    pvp_value,
    resolve_slug,
)

if TYPE_CHECKING:
    from sidequest_bot.api_client import SideQuestAPI

logger = logging.getLogger(__name__)

UNAVAILABLE_MESSAGE = "Battle.net did not answer; try again in a minute."
GEAR_SLOT_NAME_WIDTH = 13
GEAR_COLUMN_WIDTH = 7


async def _fetch_character(api: SideQuestAPI, slug: str) -> CharacterPage | str:
    """``slug``'s character page, or a plain-text error if the lookup failed."""
    try:
        return await api.get_character(slug)
    except NotFoundError as exc:
        return exc.detail
    except APIError as exc:
        if exc.status_code == HTTPStatus.SERVICE_UNAVAILABLE:
            return UNAVAILABLE_MESSAGE
        logger.warning("Character lookup failed for %r", slug, exc_info=True)
        return "Couldn't compare those characters right now; try again shortly."


def item_level_value(a: CharacterPage, b: CharacterPage) -> str:
    """Both characters' item level, with the higher side's lead highlighted (e.g. "70 vs 65 (Nox +5)")."""
    a_text = str(a.item_level) if a.item_level is not None else "—"
    b_text = str(b.item_level) if b.item_level is not None else "—"
    if a.item_level is None or b.item_level is None:
        return f"{a_text} vs {b_text}"
    diff = a.item_level - b.item_level
    if diff == 0:
        return f"{a_text} vs {b_text} (tied)"
    leader = a.name if diff > 0 else b.name
    return f"{a_text} vs {b_text} ({leader} +{abs(diff)})"


def character_field_value(character: CharacterPage) -> str:
    """A character's one-line summary plus its PvP line, for its own side of the embed."""
    return f"{build_description(character)}\n{pvp_value(character)}"


def gear_by_slot(character: CharacterPage) -> dict[str, int | None]:
    """``slot name -> item level`` for every equipped item Battle.net reported, empty if there's no profile yet."""
    if character.sheet is None:
        return {}
    return {item.slot_name: item.item_level for item in character.sheet.gear}


def gear_diff_text(a_ilvl: int | None, b_ilvl: int | None) -> str:
    """One slot's item level difference, right-aligned: ``+5``, ``-5``, or ``—`` when either side is unknown."""
    if a_ilvl is None or b_ilvl is None:
        return "—"
    diff = a_ilvl - b_ilvl
    if diff == 0:
        return "0"
    return f"{diff:+d}"


def build_gear_table(a: CharacterPage, b: CharacterPage) -> str:
    """A compact ``Slot | A | B | Diff`` table of every slot either character has gear in, as a code block body."""
    a_gear, b_gear = gear_by_slot(a), gear_by_slot(b)
    slots = sorted(set(a_gear) | set(b_gear))
    if not slots:
        return "No gear data for either character yet."
    header = (
        f"{'Slot':<{GEAR_SLOT_NAME_WIDTH}}{truncate(a.name, GEAR_COLUMN_WIDTH - 1):>{GEAR_COLUMN_WIDTH}}"
        f"{truncate(b.name, GEAR_COLUMN_WIDTH - 1):>{GEAR_COLUMN_WIDTH}}{'Diff':>{GEAR_COLUMN_WIDTH}}"
    )
    rows = [header, "-" * len(header)]
    for slot in slots:
        a_ilvl, b_ilvl = a_gear.get(slot), b_gear.get(slot)
        a_text = str(a_ilvl) if a_ilvl is not None else "—"
        b_text = str(b_ilvl) if b_ilvl is not None else "—"
        rows.append(
            f"{slot:<{GEAR_SLOT_NAME_WIDTH}}{a_text:>{GEAR_COLUMN_WIDTH}}{b_text:>{GEAR_COLUMN_WIDTH}}"
            f"{gear_diff_text(a_ilvl, b_ilvl):>{GEAR_COLUMN_WIDTH}}"
        )
    return truncate("\n".join(rows), 1000)


def build_compare_embed(a: CharacterPage, b: CharacterPage) -> discord.Embed:
    """``/compare``'s embed: level/class/PvP side by side, item level with the lead highlighted, and a gear table."""
    embed = make_embed(title=f"{a.name} {a.surname} vs {b.name} {b.surname}", color=Colors.SITE_GOLD)
    embed.add_field(name=f"{a.name} {a.surname}", value=character_field_value(a), inline=True)
    embed.add_field(name=f"{b.name} {b.surname}", value=character_field_value(b), inline=True)
    embed.add_field(name="Item level", value=item_level_value(a, b), inline=False)
    embed.add_field(name="Gear by slot", value=f"```\n{build_gear_table(a, b)}\n```", inline=False)
    return embed


class CompareCog(commands.Cog):
    """``/compare`` character comparison."""

    def __init__(self, bot: SideQuestBot) -> None:
        self.bot = bot

    @app_commands.command(name="compare", description="Compare two WoW: Forever characters' gear and stats.")
    @app_commands.describe(
        character1="First character, name and surname", character2="Second character, name and surname"
    )
    @app_commands.autocomplete(character1=_autocomplete_character, character2=_autocomplete_character)
    async def compare_command(
        self, interaction: discord.Interaction[SideQuestBot], character1: str, character2: str
    ) -> None:
        """Look up both characters concurrently and show them side by side."""
        slug1 = resolve_slug(character1)
        slug2 = resolve_slug(character2)
        if slug1 is None or slug2 is None:
            await interaction.response.send_message(NAME_HINT, ephemeral=True)
            return
        await interaction.response.defer()
        first, second = await asyncio.gather(
            _fetch_character(self.bot.api, slug1), _fetch_character(self.bot.api, slug2)
        )
        if isinstance(first, str):
            await interaction.followup.send(first, ephemeral=True)
            return
        if isinstance(second, str):
            await interaction.followup.send(second, ephemeral=True)
            return
        await interaction.followup.send(embed=build_compare_embed(first, second))


async def setup(bot: SideQuestBot) -> None:
    """Register the cog."""
    await bot.add_cog(CompareCog(bot))
