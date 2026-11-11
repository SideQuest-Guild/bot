"""Crafter search: ``/crafter <query> [profession]`` finds guild characters who know a matching recipe.

Calls the bot-key route ``GET /api/guilds/{guild_id}/crafters``, the same data the website's own ``/api/guild/crafters``
shows members, including characters synced from Battle.net who never ran the addon.
"""

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from sidequest_bot.api_client import APIError, GuildCrafters, NotFoundError
from sidequest_bot.bot import SideQuestBot  # noqa: TC001  # discord.py evaluates app-command annotations at runtime
from sidequest_bot.lib.embeds import Colors, make_embed, truncate

if TYPE_CHECKING:
    from sidequest_common.schemas import GuildCrafterResult, GuildCrafterRow

logger = logging.getLogger(__name__)

MAX_AUTOCOMPLETE_RESULTS = 25
MAX_CRAFTER_RESULTS = 10
MAX_CRAFTERS_PER_RECIPE = 5
NOT_DEPLOYED_MESSAGE = "Crafter search isn't available yet; try again later."
NO_RESULTS_MESSAGE = "No crafters found matching that search."
UNAVAILABLE_MESSAGE = "Couldn't look up crafters right now; try again shortly."


def character_label(key: str) -> str:
    """``nox-zul`` -> ``Nox Zul``: the character's slug/key, title-cased for display."""
    return " ".join(part.capitalize() for part in key.split("-"))


def crafter_line(row: GuildCrafterRow) -> str:
    """One crafter line: ``Name — Profession Skill``."""
    return f"{character_label(row.character)} — {row.profession} {row.skill}"


def build_result_field(result: GuildCrafterResult) -> str:
    """Up to :data:`MAX_CRAFTERS_PER_RECIPE` crafters for one matched recipe, one per line."""
    shown = result.crafters[:MAX_CRAFTERS_PER_RECIPE]
    lines = [crafter_line(row) for row in shown]
    extra = len(result.crafters) - len(shown)
    if extra > 0:
        lines.append(f"…and {extra} more")
    return truncate("\n".join(lines))


def build_crafters_embed(crafters: GuildCrafters, *, query: str | None, profession: str | None) -> discord.Embed:
    """One field per matched recipe (up to :data:`MAX_CRAFTER_RESULTS`), each listing who can craft it."""
    bits = [bit for bit in (f'"{query}"' if query else None, profession) if bit]
    embed = make_embed(title="Crafters", description=" · ".join(bits) if bits else None, color=Colors.SITE_GOLD)
    shown = crafters.results[:MAX_CRAFTER_RESULTS]
    for result in shown:
        embed.add_field(name=truncate(result.recipe_name, 256), value=build_result_field(result), inline=False)
    if len(crafters.results) > len(shown):
        embed.set_footer(text=f"{len(crafters.results)} recipes matched; showing the first {len(shown)}.")
    return embed


async def _autocomplete_profession(
    interaction: discord.Interaction[SideQuestBot], current: str
) -> list[app_commands.Choice[str]]:
    """The guild's known professions matching ``current``, from the crafter directory's profession counts."""
    if interaction.guild_id is None:
        return []
    try:
        crafters = await interaction.client.api.get_crafters(interaction.guild_id)
    except APIError:
        return []
    current_fold = current.casefold()
    matches = [count.profession for count in crafters.professions if current_fold in count.profession.casefold()]
    return [app_commands.Choice(name=profession, value=profession) for profession in matches[:MAX_AUTOCOMPLETE_RESULTS]]


class CraftersCog(commands.Cog):
    """``/crafter`` recipe/crafter lookup."""

    def __init__(self, bot: SideQuestBot) -> None:
        self.bot = bot

    @app_commands.command(name="crafter", description="Find guild characters who can craft an item or recipe.")
    @app_commands.describe(query="Recipe or item name to search for", profession="Filter to one profession")
    @app_commands.autocomplete(profession=_autocomplete_profession)
    @app_commands.guild_only()
    async def crafter_command(
        self, interaction: discord.Interaction[SideQuestBot], query: str, profession: str | None = None
    ) -> None:
        """Look up guild characters who know a recipe matching ``query`` (optionally filtered to ``profession``)."""
        if interaction.guild_id is None:
            return
        await interaction.response.defer()
        try:
            crafters = await self.bot.api.get_crafters(interaction.guild_id, q=query, profession=profession)
        except NotFoundError:
            await interaction.followup.send(NOT_DEPLOYED_MESSAGE, ephemeral=True)
            return
        except APIError:
            logger.warning("Crafter search failed for %r/%r", query, profession, exc_info=True)
            await interaction.followup.send(UNAVAILABLE_MESSAGE, ephemeral=True)
            return
        if not crafters.results:
            await interaction.followup.send(NO_RESULTS_MESSAGE, ephemeral=True)
            return
        await interaction.followup.send(embed=build_crafters_embed(crafters, query=query, profession=profession))


async def setup(bot: SideQuestBot) -> None:
    """Register the cog."""
    await bot.add_cog(CraftersCog(bot))
