"""Character lookup: ``/who <character>`` shows a roster or stored character's Battle.net summary and gear.

The character schemas aren't in the ``sidequest-common`` pin yet, so :mod:`sidequest_bot.api_client` defines them
locally, the same way it does for items.
"""

import logging
import re
from http import HTTPStatus
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from sidequest_bot.api_client import APIError, CharacterPage, CharacterSummary, NotFoundError
from sidequest_bot.bot import SideQuestBot  # noqa: TC001  # discord.py evaluates app-command annotations at runtime
from sidequest_bot.lib.embeds import Colors, link_button, link_view, make_embed, truncate

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sidequest_bot.api_client import CharacterGearItem, CharacterHunterPet

logger = logging.getLogger(__name__)

CHARACTER_PAGE_URL = "https://sidequest-guild.com/characters/detail/?slug={slug}"
ARMORY_URL = "https://worldofwarcraft.blizzard.com/en-us/forever/us/armory/character/{slug}"
SLUG_PATTERN = re.compile(r"^[^\s/?#-]+-[^\s/?#-]+$")
"""``name-surname``: two parts, no whitespace, slashes or hyphens inside a part. Mirrors the website's pattern."""
SEARCH_MIN_LENGTH = 2
MAX_AUTOCOMPLETE_RESULTS = 25
MAX_GEAR_FIELD_ITEMS = 8
MAX_PETS_FIELD_ITEMS = 10
NAME_HINT = "Give a name and surname, like Nox Zul"
UNAVAILABLE_MESSAGE = "Battle.net did not answer; try again in a minute."
NO_PROFILE_MESSAGE = "Battle.net hasn't published this character's profile yet."
NAME_SURNAME_PARTS = 2

CLASS_COLORS: dict[str, discord.Colour] = {
    "warrior": discord.Colour(0xC79C6E),
    "paladin": discord.Colour(0xF58CBA),
    "hunter": discord.Colour(0xABD473),
    "rogue": discord.Colour(0xFFF569),
    "priest": discord.Colour(0xFFFFFF),
    "shaman": discord.Colour(0x0070DE),
    "mage": discord.Colour(0x69CCF0),
    "warlock": discord.Colour(0x9482C9),
    "druid": discord.Colour(0xFF7D0A),
}
"""Classic WoW class colors, keyed by the API's lowercase class values."""


def class_color(wow_class: str | None) -> discord.Colour:
    """``wow_class``'s WoW color, or the house slate when unknown or unset."""
    if wow_class is None:
        return Colors.SLATE
    return CLASS_COLORS.get(wow_class, Colors.SLATE)


def resolve_slug(value: str) -> str | None:
    """A character slug from ``value``: an existing slug (from autocomplete) or two space-separated words.

    Two words are lowercased and joined with a hyphen (``"Nox Zul"`` becomes ``"nox-zul"``). ``None`` if ``value``
    is neither, so the caller should show :data:`NAME_HINT`.
    """
    value = value.strip()
    if SLUG_PATTERN.match(value):
        return value.lower()
    parts = value.split()
    if len(parts) == NAME_SURNAME_PARTS:
        return "-".join(parts).lower()
    return None


def character_choice(character: CharacterSummary) -> app_commands.Choice[str]:
    """One search result as an autocomplete choice: ``Name Surname · Level N class``, value is the slug."""
    class_part = f" {character.wow_class}" if character.wow_class else ""
    name = f"{character.name} {character.surname} · Level {character.level}{class_part}"
    return app_commands.Choice(name=truncate(name, 100), value=character.slug)


async def _autocomplete_character(
    interaction: discord.Interaction[SideQuestBot], current: str
) -> list[app_commands.Choice[str]]:
    """Roster characters (no text), or up to 25 stored characters matching ``current`` (2+ chars)."""
    current = current.strip()
    if current and len(current) < SEARCH_MIN_LENGTH:
        return []
    try:
        characters = await interaction.client.api.search_characters(current or None)
    except APIError:
        return []
    return [character_choice(character) for character in characters[:MAX_AUTOCOMPLETE_RESULTS]]


def guild_value(character: CharacterSummary) -> str:
    """The character's guild, with its rank name when the character is on the current roster."""
    if character.guild_name is None:
        return "No guild"
    if character.in_roster and character.rank_name:
        return f"{character.guild_name} ({character.rank_name})"
    return character.guild_name


def pvp_value(character: CharacterSummary) -> str:
    """Honorable kills and PvP rank, as one field; ``—`` for whichever Battle.net hasn't reported."""
    kills = character.honorable_kills if character.honorable_kills is not None else "—"
    rank = character.pvp_rank if character.pvp_rank is not None else "—"
    return f"{kills} honorable kills · PvP rank {rank}"


def gear_line(item: CharacterGearItem) -> str:
    """One gear line: ``Slot: Name (ilvl)``, or without the item level when Battle.net didn't report one."""
    ilvl = f" ({item.item_level})" if item.item_level is not None else ""
    return f"{item.slot_name}: {item.name}{ilvl}"


def build_gear_field(gear: Sequence[CharacterGearItem]) -> str:
    """Up to :data:`MAX_GEAR_FIELD_ITEMS` best items by item level, one per line."""
    ranked = sorted(gear, key=lambda item: item.item_level or 0, reverse=True)[:MAX_GEAR_FIELD_ITEMS]
    return truncate("\n".join(gear_line(item) for item in ranked))


def hunter_pet_line(pet: CharacterHunterPet) -> str:
    """One pet line: ``Name (Creature) Level``, with `` · active`` for the hunter's current pet."""
    suffix = " · active" if pet.active else ""
    return f"{pet.name} ({pet.creature}) {pet.level}{suffix}"


def build_pets_field(hunter_pets: Sequence[CharacterHunterPet]) -> str:
    """Up to :data:`MAX_PETS_FIELD_ITEMS` pets, active first, in the order Battle.net reported them."""
    return truncate("\n".join(hunter_pet_line(pet) for pet in hunter_pets[:MAX_PETS_FIELD_ITEMS]))


def build_description(character: CharacterSummary) -> str:
    """``Level N Race Class · Spec``, degrading gracefully when a part is missing or no profile exists yet."""
    if not character.profile_found:
        return NO_PROFILE_MESSAGE
    class_name = character.wow_class.capitalize() if character.wow_class else None
    header = " ".join(part for part in (f"Level {character.level}", character.race, class_name) if part)
    return f"{header} · {character.spec}" if character.spec else header


def build_character_embed(character: CharacterPage) -> discord.Embed:
    """``/who``'s embed: class-colored, title linking to the character page, roster/Battle.net summary and gear."""
    embed = make_embed(
        title=f"{character.name} {character.surname}",
        description=build_description(character),
        color=class_color(character.wow_class),
    )
    embed.url = CHARACTER_PAGE_URL.format(slug=character.slug)
    embed.add_field(name="Guild", value=guild_value(character), inline=False)
    if character.item_level is not None:
        embed.add_field(name="Item level", value=str(character.item_level))
    if character.last_login_at is not None:
        embed.add_field(name="Last login", value=discord.utils.format_dt(character.last_login_at, style="R"))
    if character.honorable_kills is not None or character.pvp_rank is not None:
        embed.add_field(name="PvP", value=pvp_value(character), inline=False)
    if character.sheet is not None and character.sheet.gear:
        embed.add_field(name="Gear", value=build_gear_field(character.sheet.gear), inline=False)
    if character.sheet is not None and character.sheet.hunter_pets:
        embed.add_field(name="Pets", value=build_pets_field(character.sheet.hunter_pets), inline=False)
    return embed


def character_link_view(slug: str) -> discord.ui.View:
    """A view holding the character's Armory link button."""
    return link_view(link_button("View on Armory", ARMORY_URL.format(slug=slug)))


class CharactersCog(commands.Cog):
    """``/who`` character lookup."""

    def __init__(self, bot: SideQuestBot) -> None:
        self.bot = bot

    @app_commands.command(name="who", description="Look up a WoW: Forever character.")
    @app_commands.describe(character="Character name and surname, like Nox Zul")
    @app_commands.autocomplete(character=_autocomplete_character)
    async def who_command(self, interaction: discord.Interaction[SideQuestBot], character: str) -> None:
        """Look up ``character`` and show its roster/Battle.net summary. Public reply on success."""
        slug = resolve_slug(character)
        if slug is None:
            await interaction.response.send_message(NAME_HINT, ephemeral=True)
            return
        await interaction.response.defer()
        try:
            page = await self.bot.api.get_character(slug)
        except NotFoundError as exc:
            await interaction.followup.send(exc.detail, ephemeral=True)
            return
        except APIError as exc:
            if exc.status_code == HTTPStatus.SERVICE_UNAVAILABLE:
                await interaction.followup.send(UNAVAILABLE_MESSAGE, ephemeral=True)
                return
            logger.warning("Character lookup failed for %r", slug, exc_info=True)
            await interaction.followup.send(
                "Couldn't look up that character right now; try again shortly.", ephemeral=True
            )
            return
        await interaction.followup.send(embed=build_character_embed(page), view=character_link_view(page.slug))


async def setup(bot: SideQuestBot) -> None:
    """Register the cog."""
    await bot.add_cog(CharactersCog(bot))
