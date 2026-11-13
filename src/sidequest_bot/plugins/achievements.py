"""Achievement lookup: ``/achievement <name>`` shows a WoW: Forever achievement's points, description and category.

Battle.net's achievement API has no name search, so this cog flattens the category tree (``GET
/api/achievements/categories`` then ``GET /api/achievements/categories/{id}`` for each category) into one list,
cached at module scope for :data:`CACHE_TTL` — the same hour the site itself caches this Battle.net-backed reference
data at the edge (``EDGE_REFERENCE_CACHE`` in the website's ``reference/controllers.py``).
"""

import asyncio
import logging
from datetime import datetime, timedelta
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

    from sidequest_bot.api_client import SideQuestAPI
    from sidequest_common.schemas import AchievementSchema, AchievementSummary

logger = logging.getLogger(__name__)

ACHIEVEMENT_PAGE_URL = "https://sidequest-guild.com/achievements/detail/?id={achievement_id}"
ACHIEVEMENT_WOWHEAD_URL = "https://www.wowhead.com/forever/achievement={achievement_id}"
DESCRIPTION_LIMIT = 4096
MAX_AUTOCOMPLETE_RESULTS = 25
MAX_OTHER_MATCHES = 5
CACHE_TTL = timedelta(hours=1)
"""How long the flattened achievement catalog is cached; matches the site's own edge cache for this data."""
NOT_FOUND_HINT = "Try a shorter word, or pick a suggestion as you type."
UNAVAILABLE_MESSAGE = "Achievement lookup isn't available right now; try again shortly."


def build_achievement_embed(achievement: AchievementSchema) -> discord.Embed:
    """The achievement's name, points, description, category and icon, linking to its site page."""
    embed = make_embed(
        title=achievement.name,
        description=truncate(achievement.description, DESCRIPTION_LIMIT),
        color=Colors.SITE_GOLD,
        timestamp=False,
    )
    embed.url = ACHIEVEMENT_PAGE_URL.format(achievement_id=achievement.id)
    if achievement.icon_url:
        embed.set_thumbnail(url=achievement.icon_url)
    embed.add_field(name="Points", value=str(achievement.points))
    if achievement.category:
        embed.add_field(name="Category", value=achievement.category)
    if achievement.reward_description:
        embed.add_field(name="Reward", value=truncate(achievement.reward_description), inline=False)
    return embed


def achievement_link_buttons(achievement_id: int) -> list[discord.ui.Button]:
    """Site + Wowhead link buttons for one achievement."""
    return [
        link_button("Site", ACHIEVEMENT_PAGE_URL.format(achievement_id=achievement_id)),
        link_button("Wowhead", ACHIEVEMENT_WOWHEAD_URL.format(achievement_id=achievement_id)),
    ]


def achievement_link_view(achievement_id: int) -> discord.ui.View:
    """A view holding just the achievement's link buttons, for replies with no other matches to offer."""
    return link_view(*achievement_link_buttons(achievement_id))


def match_option(match: AchievementSummary) -> discord.SelectOption:
    """One search result as a select option, with its points as the description."""
    return discord.SelectOption(label=truncate(match.name, 100), value=str(match.id), description=f"{match.points} pts")


class _AchievementCache:
    """The flattened achievement catalog, rebuilt at most once per :data:`CACHE_TTL`."""

    def __init__(self) -> None:
        self._achievements: list[AchievementSummary] = []
        self._built_at: datetime | None = None
        self._lock = asyncio.Lock()

    async def get(self, api: SideQuestAPI) -> list[AchievementSummary]:
        """The cached catalog, rebuilding it first if stale or never built."""
        async with self._lock:
            now = discord.utils.utcnow()
            if self._built_at is None or now - self._built_at > CACHE_TTL:
                self._achievements = await self._build(api)
                self._built_at = now
            return self._achievements

    @staticmethod
    async def _build(api: SideQuestAPI) -> list[AchievementSummary]:
        """Every achievement in every category, fetched concurrently; a category Battle.net can't answer is skipped."""
        roots = await api.list_achievement_categories()
        category_ids = [root.id for root in roots] + [child.id for root in roots for child in root.children]
        pages = await asyncio.gather(
            *(api.get_achievement_category(category_id) for category_id in category_ids), return_exceptions=True
        )
        achievements: list[AchievementSummary] = []
        for page in pages:
            if isinstance(page, APIError):
                continue
            if isinstance(page, BaseException):
                raise page
            achievements.extend(page.achievements)
        return achievements


_cache = _AchievementCache()


async def _autocomplete_achievement(
    interaction: discord.Interaction[SideQuestBot], current: str
) -> list[app_commands.Choice[str]]:
    """Up to 25 achievements matching ``current``, from the cached catalog; empty if Battle.net can't be reached."""
    try:
        achievements = await _cache.get(interaction.client.api)
    except APIError:
        return []
    current_fold = current.strip().casefold()
    if current_fold:
        achievements = [a for a in achievements if current_fold in a.name.casefold()]
    return [
        app_commands.Choice(name=truncate(f"{a.name} ({a.points} pts)", 100), value=str(a.id))
        for a in achievements[:MAX_AUTOCOMPLETE_RESULTS]
    ]


async def _fetch_achievement_embed(
    interaction: discord.Interaction[SideQuestBot], achievement_id: int
) -> discord.Embed | str:
    """The achievement's embed, or a plain-text error if the lookup failed."""
    try:
        found = await interaction.client.api.get_achievement(achievement_id)
    except NotFoundError:
        return "That achievement is gone."
    except APIError as exc:
        if exc.status_code == HTTPStatus.SERVICE_UNAVAILABLE:
            return UNAVAILABLE_MESSAGE
        logger.warning("Achievement lookup failed for %s", achievement_id, exc_info=True)
        return "Couldn't look up that achievement right now; try again shortly."
    return build_achievement_embed(found)


class _AchievementMatchSelect(discord.ui.Select["AchievementMatchView"]):
    """The select menu itself; its callback swaps the parent view's embed to the chosen achievement."""

    def __init__(self, matches: Sequence[AchievementSummary]) -> None:
        super().__init__(placeholder="Other matches", options=[match_option(match) for match in matches])

    async def callback(self, interaction: discord.Interaction[SideQuestBot]) -> None:
        achievement_id = int(self.values[0])
        result = await _fetch_achievement_embed(interaction, achievement_id)
        if isinstance(result, str):
            await interaction.response.send_message(result, ephemeral=True)
            return
        view = cast("AchievementMatchView", self.view)
        view.set_achievement(achievement_id)
        await interaction.response.edit_message(embed=result, view=view)


class AchievementMatchView(discord.ui.View):
    """Link buttons for the shown achievement plus a select menu of up to :data:`MAX_OTHER_MATCHES` other matches.

    Picking another match swaps the shown embed and repoints the link buttons at it.
    """

    def __init__(self, achievement_id: int, matches: Sequence[AchievementSummary]) -> None:
        super().__init__(timeout=180)
        self._site_button, self._wowhead_button = achievement_link_buttons(achievement_id)
        self._select = _AchievementMatchSelect(matches)
        self.add_item(self._select)
        self.add_item(self._site_button)
        self.add_item(self._wowhead_button)

    def set_achievement(self, achievement_id: int) -> None:
        """Repoint the link buttons at ``achievement_id``."""
        self._site_button.url = ACHIEVEMENT_PAGE_URL.format(achievement_id=achievement_id)
        self._wowhead_button.url = ACHIEVEMENT_WOWHEAD_URL.format(achievement_id=achievement_id)


class AchievementsCog(commands.Cog):
    """``/achievement`` lookup."""

    def __init__(self, bot: SideQuestBot) -> None:
        self.bot = bot

    @app_commands.command(name="achievement", description="Look up a WoW: Forever achievement.")
    @app_commands.describe(name="Achievement name, or pick a suggestion as you type")
    @app_commands.autocomplete(name=_autocomplete_achievement)
    async def achievement_command(self, interaction: discord.Interaction[SideQuestBot], name: str) -> None:
        """Resolve ``name`` to an achievement id (from autocomplete, or a name search) and show its embed."""
        await interaction.response.defer()
        if name.isdigit():
            result = await _fetch_achievement_embed(interaction, int(name))
            if isinstance(result, str):
                await interaction.followup.send(result, ephemeral=True)
                return
            await interaction.followup.send(embed=result, view=achievement_link_view(int(name)))
            return
        try:
            achievements = await _cache.get(self.bot.api)
        except APIError as exc:
            if exc.status_code == HTTPStatus.SERVICE_UNAVAILABLE:
                await interaction.followup.send(UNAVAILABLE_MESSAGE, ephemeral=True)
                return
            logger.warning("Achievement catalog failed to build", exc_info=True)
            await interaction.followup.send(
                "Couldn't search achievements right now; try again shortly.", ephemeral=True
            )
            return
        name_fold = name.casefold()
        matches = [a for a in achievements if name_fold in a.name.casefold()]
        if not matches:
            await interaction.followup.send(
                f"No achievements found matching **{name}**. {NOT_FOUND_HINT}", ephemeral=True
            )
            return
        best, others = matches[0], matches[1 : 1 + MAX_OTHER_MATCHES]
        result = await _fetch_achievement_embed(interaction, best.id)
        if isinstance(result, str):
            await interaction.followup.send(result, ephemeral=True)
            return
        if others:
            await interaction.followup.send(embed=result, view=AchievementMatchView(best.id, others))
        else:
            await interaction.followup.send(embed=result, view=achievement_link_view(best.id))


async def setup(bot: SideQuestBot) -> None:
    """Register the cog."""
    await bot.add_cog(AchievementsCog(bot))
