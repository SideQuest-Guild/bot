"""Quest lookup: ``/quest <name>`` shows a WoW: Forever quest's rewards, requirements and where it sits in its chain.

The website's quest search (``GET /api/quests/search``) matches any substring of a title, case-insensitive, so
autocomplete calls it live rather than flattening a catalog the way :mod:`sidequest_bot.plugins.achievements` does.
"""

import logging
from http import HTTPStatus
from typing import TYPE_CHECKING, cast

import discord
from discord import app_commands
from discord.ext import commands

from sidequest_bot.api_client import APIError, NotFoundError
from sidequest_bot.bot import SideQuestBot  # noqa: TC001  # discord.py evaluates app-command annotations at runtime
from sidequest_bot.lib.embeds import Colors, link_button, link_view, make_embed, truncate
from sidequest_common.schemas import QUEST_SEARCH_LIMIT, QUEST_SEARCH_MIN_LENGTH

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sidequest_common.schemas import QuestRequirementRef, QuestRewardsSchema, QuestSchema, QuestSearchResult

logger = logging.getLogger(__name__)

QUEST_PAGE_URL = "https://sidequest-guild.com/quests/detail/?id={quest_id}"
QUEST_WOWHEAD_URL = "https://www.wowhead.com/forever/quest={quest_id}"
DESCRIPTION_LIMIT = 4096
FIELD_LIMIT = 1024
MAX_OTHER_MATCHES = 5
MAX_CHAIN_ENTRIES = 6
"""Chain entries shown on each side of the current quest before collapsing the rest into a "... N earlier" count."""
NOT_FOUND_HINT = "Try a shorter word, or pick a suggestion as you type."
UNAVAILABLE_MESSAGE = "Quest lookup isn't available right now; try again shortly."


def format_money(copper: int) -> str:
    """``copper`` as ``Ng Ss Cc``, matching how the website shows quest rewards."""
    gold, rest = divmod(copper, 10000)
    silver, bronze = divmod(rest, 100)
    return f"{gold}g {silver}s {bronze}c"


def _level_range(min_level: int | None, max_level: int | None) -> str | None:
    """``min-max``, or just the one level known; ``None`` if neither is set."""
    if min_level is not None and max_level is not None and min_level != max_level:
        return f"{min_level}-{max_level}"
    level = min_level if min_level is not None else max_level
    return str(level) if level is not None else None


def _reward_lines(rewards: QuestRewardsSchema) -> list[str]:
    """XP, money, items and reputation a quest grants, one per line."""
    lines: list[str] = []
    if rewards.experience:
        lines.append(f"{rewards.experience} XP")
    if rewards.money:
        lines.append(format_money(rewards.money))
    lines.extend(item.name for item in rewards.items)
    lines.extend(f"{'+' if rep.value >= 0 else ''}{rep.value} {rep.faction}" for rep in rewards.reputations)
    return lines


def _chain_text(quest: QuestSchema) -> str | None:
    """``earlier quests → **this quest** → later quests``, collapsing overflow on either side."""
    if not quest.chain and not quest.leads_to:
        return None
    chain = quest.chain
    segments: list[str] = []
    if len(chain) > MAX_CHAIN_ENTRIES:
        segments.append(f"… {len(chain) - MAX_CHAIN_ENTRIES} earlier")
        chain = chain[-MAX_CHAIN_ENTRIES:]
    segments.extend(truncate(ref.title, 80) for ref in chain)
    segments.append(f"**{truncate(quest.title, 80)}**")
    leads_to = quest.leads_to[:MAX_CHAIN_ENTRIES]
    segments.extend(truncate(ref.title, 80) for ref in leads_to)
    if len(quest.leads_to) > MAX_CHAIN_ENTRIES:
        segments.append(f"… {len(quest.leads_to) - MAX_CHAIN_ENTRIES} more")
    return " → ".join(segments)


def _requires_text(requires: Sequence[QuestRequirementRef]) -> str | None:
    """One line per requirement; quests sharing a ``group`` are joined with "or"."""
    if not requires:
        return None
    lines: list[str] = []
    seen_groups: set[int] = set()
    for req in requires:
        if req.group is None:
            lines.append(req.title)
        elif req.group not in seen_groups:
            seen_groups.add(req.group)
            alts = [other.title for other in requires if other.group == req.group]
            lines.append(" or ".join(alts))
    return "\n".join(lines)


def build_quest_embed(quest: QuestSchema) -> discord.Embed:
    """The quest's area/category/type, level, rewards, chain and requirements, linking to its site page."""
    embed = make_embed(
        title=quest.title,
        description=truncate(quest.description, DESCRIPTION_LIMIT) if quest.description else None,
        color=Colors.GOLD,
        timestamp=False,
    )
    embed.url = QUEST_PAGE_URL.format(quest_id=quest.id)

    meta = [part for part in (quest.area, quest.category, quest.type) if part]
    if meta:
        embed.add_field(name="Area / Category / Type", value=" • ".join(meta), inline=False)

    level = _level_range(quest.min_level, quest.max_level)
    if level:
        embed.add_field(name="Level", value=level)

    repeats = [
        label
        for label, flag in (("Daily", quest.is_daily), ("Weekly", quest.is_weekly), ("Repeatable", quest.is_repeatable))
        if flag
    ]
    if repeats:
        embed.add_field(name="Repeats", value=", ".join(dict.fromkeys(repeats)))

    reward_lines = _reward_lines(quest.rewards)
    if reward_lines:
        embed.add_field(name="Rewards", value=truncate("\n".join(reward_lines), FIELD_LIMIT), inline=False)

    chain_text = _chain_text(quest)
    if chain_text:
        embed.add_field(name="Chain", value=truncate(chain_text, FIELD_LIMIT), inline=False)

    requires_text = _requires_text(quest.requires)
    if requires_text:
        embed.add_field(name="Requires", value=truncate(requires_text, FIELD_LIMIT), inline=False)

    if quest.excludes:
        excludes_text = "\n".join(ref.title for ref in quest.excludes)
        embed.add_field(name="Not available if you've done", value=truncate(excludes_text, FIELD_LIMIT), inline=False)

    return embed


def quest_link_buttons(quest_id: int) -> list[discord.ui.Button]:
    """Quest page + Wowhead link buttons for one quest."""
    return [
        link_button("Quest page", QUEST_PAGE_URL.format(quest_id=quest_id)),
        link_button("Wowhead", QUEST_WOWHEAD_URL.format(quest_id=quest_id)),
    ]


def quest_link_view(quest_id: int) -> discord.ui.View:
    """A view holding just the quest's link buttons, for replies with no other matches to offer."""
    return link_view(*quest_link_buttons(quest_id))


def match_option(match: QuestSearchResult) -> discord.SelectOption:
    """One search result as a select option, with its area as the description."""
    return discord.SelectOption(
        label=truncate(match.title, 100),
        value=str(match.id),
        description=truncate(match.area, 100) if match.area else None,
    )


def autocomplete_label(result: QuestSearchResult) -> str:
    """``title (area)``, or just the title if the quest has no area."""
    return f"{result.title} ({result.area})" if result.area else result.title


async def _autocomplete_quest(
    interaction: discord.Interaction[SideQuestBot], current: str
) -> list[app_commands.Choice[str]]:
    """Up to 25 quests matching ``current``; empty for a query shorter than the API's minimum, or if it failed."""
    current = current.strip()
    if len(current) < QUEST_SEARCH_MIN_LENGTH:
        return []
    try:
        results = await interaction.client.api.search_quests(current, limit=QUEST_SEARCH_LIMIT)
    except APIError:
        return []
    return [app_commands.Choice(name=truncate(autocomplete_label(r), 100), value=str(r.id)) for r in results]


async def _fetch_quest_embed(interaction: discord.Interaction[SideQuestBot], quest_id: int) -> discord.Embed | str:
    """The quest's embed, or a plain-text error if the lookup failed."""
    try:
        quest = await interaction.client.api.get_quest(quest_id)
    except NotFoundError:
        return "That quest is gone."
    except APIError as exc:
        if exc.status_code == HTTPStatus.SERVICE_UNAVAILABLE:
            return UNAVAILABLE_MESSAGE
        logger.warning("Quest lookup failed for %s", quest_id, exc_info=True)
        return "Couldn't look up that quest right now; try again shortly."
    return build_quest_embed(quest)


class _QuestMatchSelect(discord.ui.Select["QuestMatchView"]):
    """The select menu itself; its callback swaps the parent view's embed to the chosen quest."""

    def __init__(self, matches: Sequence[QuestSearchResult]) -> None:
        super().__init__(placeholder="Other matches", options=[match_option(match) for match in matches])

    async def callback(self, interaction: discord.Interaction[SideQuestBot]) -> None:
        quest_id = int(self.values[0])
        result = await _fetch_quest_embed(interaction, quest_id)
        if isinstance(result, str):
            await interaction.response.send_message(result, ephemeral=True)
            return
        view = cast("QuestMatchView", self.view)
        view.set_quest(quest_id)
        await interaction.response.edit_message(embed=result, view=view)


class QuestMatchView(discord.ui.View):
    """Link buttons for the shown quest plus a select menu of up to :data:`MAX_OTHER_MATCHES` other matches.

    Picking another match swaps the shown embed and repoints the link buttons at it.
    """

    def __init__(self, quest_id: int, matches: Sequence[QuestSearchResult]) -> None:
        super().__init__(timeout=180)
        self._page_button, self._wowhead_button = quest_link_buttons(quest_id)
        self._select = _QuestMatchSelect(matches)
        self.add_item(self._select)
        self.add_item(self._page_button)
        self.add_item(self._wowhead_button)

    def set_quest(self, quest_id: int) -> None:
        """Repoint the link buttons at ``quest_id``."""
        self._page_button.url = QUEST_PAGE_URL.format(quest_id=quest_id)
        self._wowhead_button.url = QUEST_WOWHEAD_URL.format(quest_id=quest_id)


class QuestsCog(commands.Cog):
    """``/quest`` lookup."""

    def __init__(self, bot: SideQuestBot) -> None:
        self.bot = bot

    @app_commands.command(name="quest", description="Look up a WoW: Forever quest.")
    @app_commands.describe(name="Quest name, or pick a suggestion as you type")
    @app_commands.autocomplete(name=_autocomplete_quest)
    async def quest_command(self, interaction: discord.Interaction[SideQuestBot], name: str) -> None:
        """Resolve ``name`` to a quest id (from autocomplete, or a title search) and show its embed."""
        await interaction.response.defer()
        if name.isdigit():
            result = await _fetch_quest_embed(interaction, int(name))
            if isinstance(result, str):
                await interaction.followup.send(result, ephemeral=True)
                return
            await interaction.followup.send(embed=result, view=quest_link_view(int(name)))
            return
        try:
            matches = await self.bot.api.search_quests(name, limit=QUEST_SEARCH_LIMIT)
        except APIError as exc:
            if exc.status_code == HTTPStatus.SERVICE_UNAVAILABLE:
                await interaction.followup.send(UNAVAILABLE_MESSAGE, ephemeral=True)
                return
            logger.warning("Quest search failed for %r", name, exc_info=True)
            await interaction.followup.send("Couldn't search quests right now; try again shortly.", ephemeral=True)
            return
        if not matches:
            await interaction.followup.send(f"No quests found matching **{name}**. {NOT_FOUND_HINT}", ephemeral=True)
            return
        best, others = matches[0], matches[1 : 1 + MAX_OTHER_MATCHES]
        result = await _fetch_quest_embed(interaction, best.id)
        if isinstance(result, str):
            await interaction.followup.send(result, ephemeral=True)
            return
        if others:
            await interaction.followup.send(embed=result, view=QuestMatchView(best.id, others))
        else:
            await interaction.followup.send(embed=result, view=quest_link_view(best.id))


async def setup(bot: SideQuestBot) -> None:
    """Register the cog."""
    await bot.add_cog(QuestsCog(bot))
