"""Unit tests for the achievements plugin: embed builders, the catalog cache, autocomplete and ``/achievement``."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

import sidequest_bot.plugins.achievements as achievements_module
from sidequest_bot.api_client import APIError, NotFoundError
from sidequest_bot.plugins.achievements import (
    CACHE_TTL,
    NOT_FOUND_HINT,
    UNAVAILABLE_MESSAGE,
    AchievementMatchView,
    AchievementsCog,
    _AchievementCache,
    _autocomplete_achievement,
    build_achievement_embed,
    match_option,
)
from sidequest_common.schemas import (
    AchievementCategorySchema,
    AchievementCategorySummary,
    AchievementCategoryTree,
    AchievementCriterion,
    AchievementSchema,
    AchievementSummary,
)


@pytest.fixture(autouse=True)
def _reset_cache() -> None:
    achievements_module._cache = achievements_module._AchievementCache()


def _summary(
    *, achievement_id: int = 1, name: str = "First Steps", points: int = 10, icon_url: str | None = None
) -> AchievementSummary:
    return AchievementSummary(id=achievement_id, name=name, points=points, icon_url=icon_url)


def _achievement(
    *,
    achievement_id: int = 1,
    name: str = "First Steps",
    description: str = "Take your first steps.",
    points: int = 10,
    icon_url: str | None = "https://render.worldofwarcraft.com/icons/56/ach.jpg",
    category: str | None = "General",
    reward_description: str | None = None,
) -> AchievementSchema:
    return AchievementSchema(
        id=achievement_id,
        name=name,
        description=description,
        points=points,
        icon_url=icon_url,
        category=category,
        reward_description=reward_description,
        criteria=AchievementCriterion(description=None, amount=1, children=[]),
    )


def _make_cog(*, api: object | None = None) -> AchievementsCog:
    cog = AchievementsCog.__new__(AchievementsCog)
    cog.bot = SimpleNamespace(api=api or AsyncMock())
    return cog


def _make_interaction(*, api: object | None = None) -> Mock:
    interaction = Mock(spec=discord.Interaction)
    interaction.response = Mock()
    interaction.response.defer = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    interaction.followup = Mock()
    interaction.followup.send = AsyncMock()
    interaction.client = SimpleNamespace(api=api or AsyncMock())
    return interaction


class TestBuildAchievementEmbed:
    def test_sets_title_color_url_and_thumbnail(self) -> None:
        embed = build_achievement_embed(_achievement(achievement_id=42, name="Explorer"))
        assert embed.title == "Explorer"
        assert embed.color == discord.Colour(0xD8AB5C)
        assert embed.url == "https://sidequest-guild.com/achievements/detail/?id=42"
        assert embed.thumbnail.url == "https://render.worldofwarcraft.com/icons/56/ach.jpg"

    def test_no_icon_leaves_thumbnail_unset(self) -> None:
        embed = build_achievement_embed(_achievement(icon_url=None))
        assert embed.thumbnail.url is None

    def test_includes_points_and_category(self) -> None:
        embed = build_achievement_embed(_achievement(points=25, category="Quests"))
        field_names = {field.name: field.value for field in embed.fields}
        assert field_names["Points"] == "25"
        assert field_names["Category"] == "Quests"

    def test_omits_category_when_unset(self) -> None:
        embed = build_achievement_embed(_achievement(category=None))
        assert "Category" not in [field.name for field in embed.fields]

    def test_includes_reward_when_present(self) -> None:
        embed = build_achievement_embed(_achievement(reward_description="A shiny title."))
        field_names = {field.name: field.value for field in embed.fields}
        assert field_names["Reward"] == "A shiny title."

    def test_omits_reward_when_absent(self) -> None:
        embed = build_achievement_embed(_achievement(reward_description=None))
        assert "Reward" not in [field.name for field in embed.fields]

    def test_site_field_links_to_detail_page(self) -> None:
        embed = build_achievement_embed(_achievement(achievement_id=7))
        field_names = {field.name: field.value for field in embed.fields}
        assert "https://sidequest-guild.com/achievements/detail/?id=7" in field_names["Site"]


class TestMatchOption:
    def test_label_value_and_description(self) -> None:
        option = match_option(_summary(achievement_id=3, name="Explorer", points=15))
        assert option.label == "Explorer"
        assert option.value == "3"
        assert option.description == "15 pts"


class TestAchievementCache:
    @staticmethod
    def _api() -> AsyncMock:
        api = AsyncMock()
        api.list_achievement_categories.return_value = [
            AchievementCategoryTree(id=1, name="Root", children=[AchievementCategorySummary(id=2, name="Child")])
        ]
        api.get_achievement_category.side_effect = lambda category_id: AchievementCategorySchema(
            id=category_id, name=f"Category {category_id}", achievements=[_summary(achievement_id=category_id)]
        )
        return api

    async def test_builds_from_category_tree(self) -> None:
        api = self._api()
        cache = _AchievementCache()

        achievements = await cache.get(api)

        api.list_achievement_categories.assert_awaited_once()
        assert api.get_achievement_category.await_count == 2
        assert {a.id for a in achievements} == {1, 2}

    async def test_second_call_within_ttl_uses_cache(self) -> None:
        api = self._api()
        cache = _AchievementCache()

        await cache.get(api)
        await cache.get(api)

        api.list_achievement_categories.assert_awaited_once()

    async def test_rebuilds_after_ttl_expires(self) -> None:
        api = self._api()
        cache = _AchievementCache()

        await cache.get(api)
        cache._built_at -= CACHE_TTL + timedelta(seconds=1)
        await cache.get(api)

        assert api.list_achievement_categories.await_count == 2

    async def test_skips_category_battlenet_cannot_answer(self) -> None:
        api = self._api()

        def side_effect(category_id: int) -> AchievementCategorySchema:
            if category_id == 2:
                raise APIError(503, "unavailable")
            return AchievementCategorySchema(id=category_id, name="Root", achievements=[_summary(achievement_id=1)])

        api.get_achievement_category.side_effect = side_effect
        cache = _AchievementCache()

        achievements = await cache.get(api)

        assert {a.id for a in achievements} == {1}

    async def test_propagates_failure_listing_categories(self) -> None:
        api = self._api()
        api.list_achievement_categories.side_effect = APIError(503, "down")
        cache = _AchievementCache()

        with pytest.raises(APIError):
            await cache.get(api)


class TestAutocompleteAchievement:
    async def test_filters_by_substring(self) -> None:
        api = AsyncMock()
        api.list_achievement_categories.return_value = [AchievementCategoryTree(id=1, name="Root", children=[])]
        api.get_achievement_category.return_value = AchievementCategorySchema(
            id=1,
            name="Root",
            achievements=[
                _summary(achievement_id=1, name="First Steps"),
                _summary(achievement_id=2, name="Second Wind"),
            ],
        )
        interaction = _make_interaction(api=api)

        choices = await _autocomplete_achievement(interaction, "first")

        assert [choice.value for choice in choices] == ["1"]

    async def test_empty_current_returns_everything(self) -> None:
        api = AsyncMock()
        api.list_achievement_categories.return_value = [AchievementCategoryTree(id=1, name="Root", children=[])]
        api.get_achievement_category.return_value = AchievementCategorySchema(
            id=1, name="Root", achievements=[_summary(achievement_id=1)]
        )
        interaction = _make_interaction(api=api)

        choices = await _autocomplete_achievement(interaction, "")

        assert len(choices) == 1

    async def test_api_error_returns_empty(self) -> None:
        api = AsyncMock()
        api.list_achievement_categories.side_effect = APIError(503, "down")
        interaction = _make_interaction(api=api)

        assert await _autocomplete_achievement(interaction, "first") == []


class TestAchievementCommand:
    async def test_digit_name_fetches_directly(self) -> None:
        api = AsyncMock()
        api.get_achievement.return_value = _achievement(achievement_id=42, name="Explorer")
        cog = _make_cog(api=api)
        interaction = _make_interaction(api=api)

        await AchievementsCog.achievement_command.callback(cog, interaction, "42")

        api.get_achievement.assert_awaited_once_with(42)
        kwargs = interaction.followup.send.await_args.kwargs
        assert kwargs["embed"].title == "Explorer"
        assert "ephemeral" not in kwargs

    async def test_digit_name_not_found(self) -> None:
        api = AsyncMock()
        api.get_achievement.side_effect = NotFoundError(404, "gone")
        cog = _make_cog(api=api)
        interaction = _make_interaction(api=api)

        await AchievementsCog.achievement_command.callback(cog, interaction, "42")

        interaction.followup.send.assert_awaited_once_with("That achievement is gone.", ephemeral=True)

    async def test_digit_name_unavailable(self) -> None:
        api = AsyncMock()
        api.get_achievement.side_effect = APIError(503, "down")
        cog = _make_cog(api=api)
        interaction = _make_interaction(api=api)

        await AchievementsCog.achievement_command.callback(cog, interaction, "42")

        interaction.followup.send.assert_awaited_once_with(UNAVAILABLE_MESSAGE, ephemeral=True)

    async def test_name_search_unavailable(self) -> None:
        api = AsyncMock()
        api.list_achievement_categories.side_effect = APIError(503, "down")
        cog = _make_cog(api=api)
        interaction = _make_interaction(api=api)

        await AchievementsCog.achievement_command.callback(cog, interaction, "explorer")

        interaction.followup.send.assert_awaited_once_with(UNAVAILABLE_MESSAGE, ephemeral=True)

    async def test_name_search_generic_error(self) -> None:
        api = AsyncMock()
        api.list_achievement_categories.side_effect = APIError(500, "boom")
        cog = _make_cog(api=api)
        interaction = _make_interaction(api=api)

        await AchievementsCog.achievement_command.callback(cog, interaction, "explorer")

        message = interaction.followup.send.await_args.args[0]
        assert "try again shortly" in message

    async def test_no_matches_sends_hint(self) -> None:
        api = AsyncMock()
        api.list_achievement_categories.return_value = []
        cog = _make_cog(api=api)
        interaction = _make_interaction(api=api)

        await AchievementsCog.achievement_command.callback(cog, interaction, "zzznotfound")

        message = interaction.followup.send.await_args.args[0]
        assert NOT_FOUND_HINT in message

    async def test_single_match_sends_embed_without_select(self) -> None:
        api = AsyncMock()
        api.list_achievement_categories.return_value = [AchievementCategoryTree(id=1, name="Root", children=[])]
        api.get_achievement_category.return_value = AchievementCategorySchema(
            id=1, name="Root", achievements=[_summary(achievement_id=7, name="Explorer")]
        )
        api.get_achievement.return_value = _achievement(achievement_id=7, name="Explorer")
        cog = _make_cog(api=api)
        interaction = _make_interaction(api=api)

        await AchievementsCog.achievement_command.callback(cog, interaction, "explorer")

        kwargs = interaction.followup.send.await_args.kwargs
        assert kwargs["embed"].title == "Explorer"
        assert "view" not in kwargs

    async def test_multiple_matches_offers_select_of_others(self) -> None:
        api = AsyncMock()
        api.list_achievement_categories.return_value = [AchievementCategoryTree(id=1, name="Root", children=[])]
        matches = [_summary(achievement_id=i, name=f"Explorer {i}") for i in range(1, 8)]
        api.get_achievement_category.return_value = AchievementCategorySchema(id=1, name="Root", achievements=matches)
        api.get_achievement.return_value = _achievement(achievement_id=1, name="Explorer 1")
        cog = _make_cog(api=api)
        interaction = _make_interaction(api=api)

        await AchievementsCog.achievement_command.callback(cog, interaction, "explorer")

        kwargs = interaction.followup.send.await_args.kwargs
        view = kwargs["view"]
        assert isinstance(view, AchievementMatchView)
        assert [option.value for option in view._select.options] == ["2", "3", "4", "5", "6"]

    async def test_achievement_gone_after_search_reports_gone(self) -> None:
        api = AsyncMock()
        api.list_achievement_categories.return_value = [AchievementCategoryTree(id=1, name="Root", children=[])]
        api.get_achievement_category.return_value = AchievementCategorySchema(
            id=1, name="Root", achievements=[_summary(achievement_id=1, name="Explorer")]
        )
        api.get_achievement.side_effect = NotFoundError(404, "gone")
        cog = _make_cog(api=api)
        interaction = _make_interaction(api=api)

        await AchievementsCog.achievement_command.callback(cog, interaction, "explorer")

        interaction.followup.send.assert_awaited_once_with("That achievement is gone.", ephemeral=True)


class TestAchievementMatchView:
    def test_options_use_match_names_and_ids(self) -> None:
        view = AchievementMatchView([_summary(achievement_id=1, name="A"), _summary(achievement_id=2, name="B")])
        assert [option.label for option in view._select.options] == ["A", "B"]
        assert [option.value for option in view._select.options] == ["1", "2"]

    async def test_select_callback_edits_message_with_chosen_achievement(self) -> None:
        api = AsyncMock()
        api.get_achievement.return_value = _achievement(achievement_id=2, name="B")
        view = AchievementMatchView([_summary(achievement_id=2, name="B")])
        view._select._values = ["2"]
        interaction = _make_interaction(api=api)

        await view._select.callback(interaction)

        interaction.response.edit_message.assert_awaited_once()
        kwargs = interaction.response.edit_message.await_args.kwargs
        assert kwargs["embed"].title == "B"
        assert kwargs["view"] is view

    async def test_select_callback_reports_error_ephemerally(self) -> None:
        api = AsyncMock()
        api.get_achievement.side_effect = NotFoundError(404, "gone")
        view = AchievementMatchView([_summary(achievement_id=2)])
        view._select._values = ["2"]
        interaction = _make_interaction(api=api)

        await view._select.callback(interaction)

        interaction.response.send_message.assert_awaited_once_with("That achievement is gone.", ephemeral=True)
        interaction.response.edit_message.assert_not_awaited()
