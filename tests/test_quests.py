"""Unit tests for the quests plugin: the embed builder, autocomplete, ``/quest``, and the match select menu."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord

from sidequest_bot.api_client import APIError, NotFoundError
from sidequest_bot.plugins.quests import (
    NOT_FOUND_HINT,
    UNAVAILABLE_MESSAGE,
    QuestMatchView,
    QuestsCog,
    _autocomplete_quest,
    autocomplete_label,
    build_quest_embed,
    format_money,
    match_option,
    quest_link_buttons,
    quest_link_view,
)
from sidequest_common.schemas import (
    QuestRef,
    QuestReputationReward,
    QuestRequirementRef,
    QuestRewardItem,
    QuestRewardsSchema,
    QuestSchema,
    QuestSearchResult,
)


def _search_result(
    *, quest_id: int = 1, title: str = "Call of Water", area: str | None = "Durotar"
) -> QuestSearchResult:
    return QuestSearchResult(id=quest_id, title=title, area=area)


def _quest(
    *,
    quest_id: int = 1,
    title: str = "Call of Water",
    description: str | None = "Bring water to the elder.",
    area: str | None = "Durotar",
    category: str | None = None,
    type: str | None = None,  # noqa: A002 - mirrors the schema's field name
    min_level: int | None = 5,
    max_level: int | None = 5,
    is_repeatable: bool = False,
    is_daily: bool = False,
    is_weekly: bool = False,
    rewards: QuestRewardsSchema | None = None,
    requires: list[QuestRequirementRef] | None = None,
    excludes: list[QuestRef] | None = None,
    chain: list[QuestRef] | None = None,
    leads_to: list[QuestRef] | None = None,
) -> QuestSchema:
    return QuestSchema(
        id=quest_id,
        title=title,
        description=description,
        area=area,
        category=category,
        type=type,
        min_level=min_level,
        max_level=max_level,
        is_repeatable=is_repeatable,
        is_daily=is_daily,
        is_weekly=is_weekly,
        rewards=rewards if rewards is not None else QuestRewardsSchema(),
        requires=requires or [],
        excludes=excludes or [],
        chain=chain or [],
        leads_to=leads_to or [],
    )


def _make_cog(*, api: object | None = None) -> QuestsCog:
    cog = QuestsCog.__new__(QuestsCog)
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


class TestFormatMoney:
    def test_splits_into_gold_silver_copper(self) -> None:
        assert format_money(123456) == "12g 34s 56c"

    def test_zero_is_all_zeros(self) -> None:
        assert format_money(0) == "0g 0s 0c"


class TestBuildQuestEmbed:
    def test_sets_title_color_and_url(self) -> None:
        embed = build_quest_embed(_quest(quest_id=42, title="Explorer's Path"))
        assert embed.title == "Explorer's Path"
        assert embed.url == "https://sidequest-guild.com/quests/detail/?id=42"

    def test_description_is_set(self) -> None:
        embed = build_quest_embed(_quest(description="Go forth."))
        assert embed.description == "Go forth."

    def test_no_description_leaves_it_unset(self) -> None:
        embed = build_quest_embed(_quest(description=None))
        assert embed.description is None

    def test_area_category_type_field(self) -> None:
        embed = build_quest_embed(_quest(area="Durotar", category="Shaman", type="Class"))
        fields = {field.name: field.value for field in embed.fields}
        assert fields["Area / Category / Type"] == "Durotar • Shaman • Class"

    def test_omits_area_field_when_all_unset(self) -> None:
        embed = build_quest_embed(_quest(area=None, category=None, type=None))
        assert "Area / Category / Type" not in [field.name for field in embed.fields]

    def test_level_range_shown_for_differing_levels(self) -> None:
        embed = build_quest_embed(_quest(min_level=10, max_level=15))
        fields = {field.name: field.value for field in embed.fields}
        assert fields["Level"] == "10-15"

    def test_single_level_shown_without_range(self) -> None:
        embed = build_quest_embed(_quest(min_level=10, max_level=10))
        fields = {field.name: field.value for field in embed.fields}
        assert fields["Level"] == "10"

    def test_omits_level_when_both_unset(self) -> None:
        embed = build_quest_embed(_quest(min_level=None, max_level=None))
        assert "Level" not in [field.name for field in embed.fields]

    def test_daily_and_weekly_flags(self) -> None:
        embed = build_quest_embed(_quest(is_daily=True, is_weekly=True))
        fields = {field.name: field.value for field in embed.fields}
        assert fields["Repeats"] == "Daily, Weekly"

    def test_omits_repeats_when_none_set(self) -> None:
        embed = build_quest_embed(_quest())
        assert "Repeats" not in [field.name for field in embed.fields]

    def test_rewards_field_includes_xp_money_items_and_reputation(self) -> None:
        rewards = QuestRewardsSchema(
            experience=500,
            money=123456,
            items=[QuestRewardItem(item_id=1, name="Worn Blade")],
            reputations=[QuestReputationReward(faction="Darkspear Trolls", value=10)],
        )
        embed = build_quest_embed(_quest(rewards=rewards))
        fields = {field.name: field.value for field in embed.fields}
        assert "500 XP" in fields["Rewards"]
        assert "12g 34s 56c" in fields["Rewards"]
        assert "Worn Blade" in fields["Rewards"]
        assert "+10 Darkspear Trolls" in fields["Rewards"]

    def test_omits_rewards_field_when_empty(self) -> None:
        embed = build_quest_embed(_quest(rewards=QuestRewardsSchema()))
        assert "Rewards" not in [field.name for field in embed.fields]

    def test_chain_field_renders_this_quest_in_bold_between_chain_and_leads_to(self) -> None:
        quest = _quest(
            title="Call of Water 3",
            chain=[QuestRef(id=1, title="Call of Water 1"), QuestRef(id=2, title="Call of Water 2")],
            leads_to=[QuestRef(id=4, title="Call of Water 4")],
        )
        embed = build_quest_embed(quest)
        fields = {field.name: field.value for field in embed.fields}
        assert fields["Chain"] == "Call of Water 1 → Call of Water 2 → **Call of Water 3** → Call of Water 4"

    def test_chain_field_collapses_earlier_overflow(self) -> None:
        chain = [QuestRef(id=i, title=f"Step {i}") for i in range(1, 10)]
        embed = build_quest_embed(_quest(title="Step 10", chain=chain))
        fields = {field.name: field.value for field in embed.fields}
        assert fields["Chain"].startswith("… 3 earlier → ")
        assert "Step 1 " not in fields["Chain"]
        assert fields["Chain"].endswith("**Step 10**")

    def test_chain_field_collapses_leads_to_overflow(self) -> None:
        leads_to = [QuestRef(id=i, title=f"Follow {i}") for i in range(1, 10)]
        embed = build_quest_embed(_quest(title="Root", leads_to=leads_to))
        fields = {field.name: field.value for field in embed.fields}
        assert fields["Chain"].endswith("… 3 more")

    def test_omits_chain_field_when_no_chain_or_leads_to(self) -> None:
        embed = build_quest_embed(_quest())
        assert "Chain" not in [field.name for field in embed.fields]

    def test_requires_field_groups_one_of_alternatives(self) -> None:
        requires = [
            QuestRequirementRef(id=1, title="Quest A", group=None),
            QuestRequirementRef(id=2, title="Quest B", group=1),
            QuestRequirementRef(id=3, title="Quest C", group=1),
        ]
        embed = build_quest_embed(_quest(requires=requires))
        fields = {field.name: field.value for field in embed.fields}
        assert fields["Requires"] == "Quest A\nQuest B or Quest C"

    def test_omits_requires_field_when_empty(self) -> None:
        embed = build_quest_embed(_quest())
        assert "Requires" not in [field.name for field in embed.fields]

    def test_excludes_field_lists_titles(self) -> None:
        embed = build_quest_embed(_quest(excludes=[QuestRef(id=9, title="Opposing Path")]))
        fields = {field.name: field.value for field in embed.fields}
        assert fields["Not available if you've done"] == "Opposing Path"

    def test_omits_excludes_field_when_empty(self) -> None:
        embed = build_quest_embed(_quest())
        assert "Not available if you've done" not in [field.name for field in embed.fields]


class TestQuestLinkButtons:
    def test_page_and_wowhead_buttons(self) -> None:
        page, wowhead = quest_link_buttons(42)
        assert page.label == "Quest page"
        assert page.url == "https://sidequest-guild.com/quests/detail/?id=42"
        assert wowhead.label == "Wowhead"
        assert wowhead.url == "https://www.wowhead.com/forever/quest=42"


class TestQuestLinkView:
    def test_holds_both_link_buttons(self) -> None:
        view = quest_link_view(42)
        labels = [button.label for button in view.children]
        assert labels == ["Quest page", "Wowhead"]


class TestMatchOption:
    def test_label_value_and_description(self) -> None:
        option = match_option(_search_result(quest_id=3, title="Explorer", area="Durotar"))
        assert option.label == "Explorer"
        assert option.value == "3"
        assert option.description == "Durotar"

    def test_no_area_leaves_description_unset(self) -> None:
        option = match_option(_search_result(area=None))
        assert option.description is None


class TestAutocompleteLabel:
    def test_includes_area_when_present(self) -> None:
        assert autocomplete_label(_search_result(title="Explorer", area="Durotar")) == "Explorer (Durotar)"

    def test_falls_back_to_title_without_area(self) -> None:
        assert autocomplete_label(_search_result(title="Explorer", area=None)) == "Explorer"


class TestAutocompleteQuest:
    async def test_short_query_returns_nothing_without_calling_the_api(self) -> None:
        api = AsyncMock()
        interaction = _make_interaction(api=api)

        choices = await _autocomplete_quest(interaction, "a")

        assert choices == []
        api.search_quests.assert_not_called()

    async def test_returns_choices_from_search(self) -> None:
        api = AsyncMock()
        api.search_quests.return_value = [_search_result(quest_id=1, title="Call of Water", area="Durotar")]
        interaction = _make_interaction(api=api)

        choices = await _autocomplete_quest(interaction, "call")

        assert [choice.value for choice in choices] == ["1"]
        assert choices[0].name == "Call of Water (Durotar)"

    async def test_api_error_returns_empty(self) -> None:
        api = AsyncMock()
        api.search_quests.side_effect = APIError(503, "down")
        interaction = _make_interaction(api=api)

        assert await _autocomplete_quest(interaction, "call") == []


class TestQuestCommand:
    async def test_digit_name_fetches_directly(self) -> None:
        api = AsyncMock()
        api.get_quest.return_value = _quest(quest_id=42, title="Explorer's Path")
        cog = _make_cog(api=api)
        interaction = _make_interaction(api=api)

        await QuestsCog.quest_command.callback(cog, interaction, "42")

        api.get_quest.assert_awaited_once_with(42)
        kwargs = interaction.followup.send.await_args.kwargs
        assert kwargs["embed"].title == "Explorer's Path"
        labels = [button.label for button in kwargs["view"].children]
        assert labels == ["Quest page", "Wowhead"]

    async def test_digit_name_not_found(self) -> None:
        api = AsyncMock()
        api.get_quest.side_effect = NotFoundError(404, "gone")
        cog = _make_cog(api=api)
        interaction = _make_interaction(api=api)

        await QuestsCog.quest_command.callback(cog, interaction, "42")

        interaction.followup.send.assert_awaited_once_with("That quest is gone.", ephemeral=True)

    async def test_digit_name_unavailable(self) -> None:
        api = AsyncMock()
        api.get_quest.side_effect = APIError(503, "down")
        cog = _make_cog(api=api)
        interaction = _make_interaction(api=api)

        await QuestsCog.quest_command.callback(cog, interaction, "42")

        interaction.followup.send.assert_awaited_once_with(UNAVAILABLE_MESSAGE, ephemeral=True)

    async def test_name_search_unavailable(self) -> None:
        api = AsyncMock()
        api.search_quests.side_effect = APIError(503, "down")
        cog = _make_cog(api=api)
        interaction = _make_interaction(api=api)

        await QuestsCog.quest_command.callback(cog, interaction, "water")

        interaction.followup.send.assert_awaited_once_with(UNAVAILABLE_MESSAGE, ephemeral=True)

    async def test_name_search_generic_error(self) -> None:
        api = AsyncMock()
        api.search_quests.side_effect = APIError(500, "boom")
        cog = _make_cog(api=api)
        interaction = _make_interaction(api=api)

        await QuestsCog.quest_command.callback(cog, interaction, "water")

        message = interaction.followup.send.await_args.args[0]
        assert "try again shortly" in message

    async def test_no_matches_sends_hint(self) -> None:
        api = AsyncMock()
        api.search_quests.return_value = []
        cog = _make_cog(api=api)
        interaction = _make_interaction(api=api)

        await QuestsCog.quest_command.callback(cog, interaction, "zzznotfound")

        message = interaction.followup.send.await_args.args[0]
        assert NOT_FOUND_HINT in message

    async def test_single_match_sends_link_buttons_without_select(self) -> None:
        api = AsyncMock()
        api.search_quests.return_value = [_search_result(quest_id=7, title="Explorer")]
        api.get_quest.return_value = _quest(quest_id=7, title="Explorer")
        cog = _make_cog(api=api)
        interaction = _make_interaction(api=api)

        await QuestsCog.quest_command.callback(cog, interaction, "explorer")

        kwargs = interaction.followup.send.await_args.kwargs
        assert kwargs["embed"].title == "Explorer"
        labels = [button.label for button in kwargs["view"].children]
        assert labels == ["Quest page", "Wowhead"]

    async def test_multiple_matches_offers_select_of_others(self) -> None:
        api = AsyncMock()
        matches = [_search_result(quest_id=i, title=f"Explorer {i}") for i in range(1, 8)]
        api.search_quests.return_value = matches
        api.get_quest.return_value = _quest(quest_id=1, title="Explorer 1")
        cog = _make_cog(api=api)
        interaction = _make_interaction(api=api)

        await QuestsCog.quest_command.callback(cog, interaction, "explorer")

        kwargs = interaction.followup.send.await_args.kwargs
        view = kwargs["view"]
        assert isinstance(view, QuestMatchView)
        assert [option.value for option in view._select.options] == ["2", "3", "4", "5", "6"]

    async def test_quest_gone_after_search_reports_gone(self) -> None:
        api = AsyncMock()
        api.search_quests.return_value = [_search_result(quest_id=1, title="Explorer")]
        api.get_quest.side_effect = NotFoundError(404, "gone")
        cog = _make_cog(api=api)
        interaction = _make_interaction(api=api)

        await QuestsCog.quest_command.callback(cog, interaction, "explorer")

        interaction.followup.send.assert_awaited_once_with("That quest is gone.", ephemeral=True)


class TestQuestMatchView:
    def test_options_use_match_titles_and_ids(self) -> None:
        view = QuestMatchView(1, [_search_result(quest_id=1, title="A"), _search_result(quest_id=2, title="B")])
        assert [option.label for option in view._select.options] == ["A", "B"]
        assert [option.value for option in view._select.options] == ["1", "2"]

    def test_link_buttons_point_at_initial_quest(self) -> None:
        view = QuestMatchView(1, [_search_result(quest_id=2, title="B")])
        assert view._page_button.url == "https://sidequest-guild.com/quests/detail/?id=1"
        assert view._wowhead_button.url == "https://www.wowhead.com/forever/quest=1"

    async def test_select_callback_edits_message_with_chosen_quest(self) -> None:
        api = AsyncMock()
        api.get_quest.return_value = _quest(quest_id=2, title="B")
        view = QuestMatchView(1, [_search_result(quest_id=2, title="B")])
        view._select._values = ["2"]
        interaction = _make_interaction(api=api)

        await view._select.callback(interaction)

        interaction.response.edit_message.assert_awaited_once()
        kwargs = interaction.response.edit_message.await_args.kwargs
        assert kwargs["embed"].title == "B"
        assert kwargs["view"] is view
        assert view._page_button.url == "https://sidequest-guild.com/quests/detail/?id=2"
        assert view._wowhead_button.url == "https://www.wowhead.com/forever/quest=2"

    async def test_select_callback_reports_error_ephemerally(self) -> None:
        api = AsyncMock()
        api.get_quest.side_effect = NotFoundError(404, "gone")
        view = QuestMatchView(1, [_search_result(quest_id=2)])
        view._select._values = ["2"]
        interaction = _make_interaction(api=api)

        await view._select.callback(interaction)

        interaction.response.send_message.assert_awaited_once_with("That quest is gone.", ephemeral=True)
        interaction.response.edit_message.assert_not_awaited()
