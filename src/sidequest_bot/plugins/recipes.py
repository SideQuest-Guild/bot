"""Recipe lookup: ``/recipe <name>`` shows a recipe's reagents, crafted item and the guild crafters who know it.

Battle.net's recipe API has no name search, so this reuses the bot-key crafters route (``GET
/api/guilds/{id}/crafters``, same data as ``/crafter``) to find the matching recipe's Battle.net id, then fetches
its full detail. If Battle.net was unreachable when the guild's crafter data last synced, that id isn't known yet;
the command then falls back to the crafters-only view (profession, skill, and who knows it, no reagents or icon).
"""

import logging
from http import HTTPStatus
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from sidequest_bot.api_client import APIError, GuildCrafters, NotFoundError
from sidequest_bot.bot import SideQuestBot  # noqa: TC001  # discord.py evaluates app-command annotations at runtime
from sidequest_bot.lib.embeds import Colors, make_embed, truncate
from sidequest_bot.plugins.crafters import character_label

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sidequest_bot.api_client import SideQuestAPI
    from sidequest_common.schemas import GuildCrafterResult, GuildCrafterRow, RecipeCrafter, RecipeDetail, RecipeReagent

logger = logging.getLogger(__name__)

MAX_REAGENTS_SHOWN = 10
MAX_CRAFTERS_SHOWN = 10
NOT_DEPLOYED_MESSAGE = "Recipe lookup isn't available yet; try again later."
NO_RESULTS_MESSAGE = "No recipes found matching that search."
UNAVAILABLE_MESSAGE = "Couldn't look up that recipe right now; try again shortly."
RECIPE_DETAIL_UNAVAILABLE_NOTE = (
    "Battle.net wasn't reachable when the guild's crafter data last synced, so this recipe's reagents and icon "
    "aren't available yet; showing who in the guild knows it."
)


def reagent_line(reagent: RecipeReagent) -> str:
    """One reagent line: ``Name x N``."""
    return f"{reagent.name} x{reagent.quantity}"


def build_reagents_field(reagents: Sequence[RecipeReagent]) -> str:
    """Up to :data:`MAX_REAGENTS_SHOWN` reagents, one per line, or a placeholder if the recipe needs none."""
    if not reagents:
        return "None"
    return truncate("\n".join(reagent_line(reagent) for reagent in reagents[:MAX_REAGENTS_SHOWN]))


def crafted_item_value(recipe: RecipeDetail) -> str:
    """What the recipe makes, with its crafted quantity text (e.g. "Makes 2-4") when Battle.net reports one."""
    if recipe.crafted_item is None:
        return "—"
    quantity = f" ({recipe.crafted_quantity})" if recipe.crafted_quantity else ""
    return f"{recipe.crafted_item.name}{quantity}"


def recipe_crafter_line(row: RecipeCrafter) -> str:
    """One crafter line: ``Name — skill N``."""
    return f"{character_label(row.character)} — skill {row.skill}"


def build_recipe_crafters_field(crafters: Sequence[RecipeCrafter]) -> str:
    """Up to :data:`MAX_CRAFTERS_SHOWN` guild crafters who know the recipe, or a placeholder if none do."""
    if not crafters:
        return "No guild crafters know this recipe yet."
    return truncate("\n".join(recipe_crafter_line(row) for row in crafters[:MAX_CRAFTERS_SHOWN]))


def build_recipe_embed(recipe: RecipeDetail) -> discord.Embed:
    """A recipe's full detail: profession, what it crafts, reagents, icon and guild crafters."""
    embed = make_embed(
        title=recipe.name,
        description=truncate(recipe.description) if recipe.description else None,
        color=Colors.SITE_GOLD,
        timestamp=False,
    )
    if recipe.icon_url:
        embed.set_thumbnail(url=recipe.icon_url)
    if recipe.profession:
        embed.add_field(name="Profession", value=recipe.profession)
    embed.add_field(name="Crafts", value=crafted_item_value(recipe))
    embed.add_field(name="Reagents", value=build_reagents_field(recipe.reagents), inline=False)
    embed.add_field(name="Guild crafters", value=build_recipe_crafters_field(recipe.crafters), inline=False)
    return embed


def partial_crafter_line(row: GuildCrafterRow) -> str:
    """One crafter line for the crafters-only fallback: ``Name — Profession Skill``."""
    return f"{character_label(row.character)} — {row.profession} {row.skill}"


def build_partial_recipe_embed(result: GuildCrafterResult) -> discord.Embed:
    """A guild-crafters-only view for when Battle.net couldn't be asked for the recipe's own detail yet."""
    embed = make_embed(
        title=result.recipe_name, description=RECIPE_DETAIL_UNAVAILABLE_NOTE, color=Colors.SITE_GOLD, timestamp=False
    )
    shown = result.crafters[:MAX_CRAFTERS_SHOWN]
    lines = [partial_crafter_line(row) for row in shown]
    embed.add_field(name="Guild crafters", value=truncate("\n".join(lines)) if lines else "None", inline=False)
    return embed


async def _search_recipe(api: SideQuestAPI, guild_id: int, name: str) -> GuildCrafters | str:
    """Matching recipes via the crafters route (reusing ``/crafter``'s data), or a plain-text error."""
    try:
        return await api.get_crafters(guild_id, q=name)
    except NotFoundError:
        return NOT_DEPLOYED_MESSAGE
    except APIError as exc:
        if exc.status_code == HTTPStatus.SERVICE_UNAVAILABLE:
            return UNAVAILABLE_MESSAGE
        logger.warning("Recipe search failed for %r", name, exc_info=True)
        return "Couldn't search recipes right now; try again shortly."


async def _fetch_recipe_embed(api: SideQuestAPI, recipe_id: int) -> discord.Embed | str | None:
    """The recipe's embed, a plain-text error, or ``None`` to fall back to the crafters-only view."""
    try:
        recipe = await api.get_recipe(recipe_id)
    except NotFoundError:
        return None
    except APIError as exc:
        if exc.status_code == HTTPStatus.SERVICE_UNAVAILABLE:
            return UNAVAILABLE_MESSAGE
        logger.warning("Recipe detail fetch failed for %s", recipe_id, exc_info=True)
        return UNAVAILABLE_MESSAGE
    return build_recipe_embed(recipe)


class RecipesCog(commands.Cog):
    """``/recipe`` lookup."""

    def __init__(self, bot: SideQuestBot) -> None:
        self.bot = bot

    @app_commands.command(name="recipe", description="Look up a WoW: Forever recipe and who in the guild can craft it.")
    @app_commands.describe(name="Recipe or item name to search for")
    @app_commands.guild_only()
    async def recipe_command(self, interaction: discord.Interaction[SideQuestBot], name: str) -> None:
        """Find the recipe matching ``name`` (via the crafters route) and show its reagents and guild crafters."""
        if interaction.guild_id is None:
            return
        await interaction.response.defer()
        crafters = await _search_recipe(self.bot.api, interaction.guild_id, name)
        if isinstance(crafters, str):
            await interaction.followup.send(crafters, ephemeral=True)
            return
        if not crafters.results:
            await interaction.followup.send(NO_RESULTS_MESSAGE, ephemeral=True)
            return
        best = crafters.results[0]
        if best.battlenet_recipe_id is not None:
            result = await _fetch_recipe_embed(self.bot.api, best.battlenet_recipe_id)
            if isinstance(result, discord.Embed):
                await interaction.followup.send(embed=result)
                return
            if isinstance(result, str):
                await interaction.followup.send(result, ephemeral=True)
                return
        await interaction.followup.send(embed=build_partial_recipe_embed(best))


async def setup(bot: SideQuestBot) -> None:
    """Register the cog."""
    await bot.add_cog(RecipesCog(bot))
