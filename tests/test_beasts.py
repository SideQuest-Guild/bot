"""Unit tests for the beasts plugin: the embed builder, autocomplete timeouts, ``/beast``, and the match select menu."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from sidequest_bot.api_client import APIError, NotFoundError
from sidequest_bot.plugins.beasts import (
    NOT_FOUND_HINT,
    UNAVAILABLE_MESSAGE,
    BeastMatchView,
    BeastsCog,
    _autocomplete_beast,
    _autocomplete_beast_family,
    autocomplete_label,
    beast_link_buttons,
    beast_link_view,
    build_beast_embed,
    match_option,
)
from sidequest_common.schemas import BeastFamilySchema, BeastSchema


def _beast(
    *,
    beast_id: int = 1,
    name: str = "Ashamane",
    family_id: int | None = 10,
    family: str | None = "Cat",
    image_url: str | None = "https://render.worldofwarcraft.com/npcs/56/beast.jpg",
    display_id: int | None = 99,
) -> BeastSchema:
    return BeastSchema(
        id=beast_id, name=name, family_id=family_id, family=family, image_url=image_url, display_id=display_id
    )


def _family(*, family_id: int = 10, name: str = "Cat", count: int = 5) -> BeastFamilySchema:
    return BeastFamilySchema(id=family_id, name=name, count=count)


def _make_cog(*, api: object | None = None) -> BeastsCog:
    cog = BeastsCog.__new__(BeastsCog)
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


class TestBuildBeastEmbed:
    def test_sets_title_and_color(self) -> None:
        embed = build_beast_embed(_beast(name="Ashamane"))
        assert embed.title == "Ashamane"

    def test_has_no_url(self) -> None:
        """No per-beast site page exists, so the embed shouldn't claim one."""
        embed = build_beast_embed(_beast())
        assert embed.url is None

    def test_family_field(self) -> None:
        embed = build_beast_embed(_beast(family="Cat"))
        fields = {field.name: field.value for field in embed.fields}
        assert fields["Family"] == "Cat"

    def test_omits_family_field_when_unset(self) -> None:
        embed = build_beast_embed(_beast(family=None))
        assert "Family" not in [field.name for field in embed.fields]

    def test_tameable_field_always_present(self) -> None:
        embed = build_beast_embed(_beast())
        fields = {field.name: field.value for field in embed.fields}
        assert fields["Tameable by hunters"] == "Yes"

    def test_sets_image_when_present(self) -> None:
        embed = build_beast_embed(_beast(image_url="https://example.test/beast.jpg"))
        assert embed.image.url == "https://example.test/beast.jpg"

    def test_no_image_leaves_image_unset(self) -> None:
        embed = build_beast_embed(_beast(image_url=None))
        assert embed.image.url is None


class TestBeastLinkButtons:
    def test_page_and_wowhead_buttons(self) -> None:
        page, wowhead = beast_link_buttons(42)
        assert page.label == "Beasts page"
        assert page.url == "https://sidequest-guild.com/beasts/"
        assert wowhead.label == "Wowhead"
        assert wowhead.url == "https://www.wowhead.com/forever/npc=42"

    def test_page_url_is_the_same_for_every_beast(self) -> None:
        page_a, _ = beast_link_buttons(1)
        page_b, _ = beast_link_buttons(2)
        assert page_a.url == page_b.url


class TestBeastLinkView:
    def test_holds_both_link_buttons(self) -> None:
        view = beast_link_view(42)
        labels = [button.label for button in view.children]
        assert labels == ["Beasts page", "Wowhead"]


class TestMatchOption:
    def test_label_value_and_description(self) -> None:
        option = match_option(_beast(beast_id=3, name="Explorer", family="Cat"))
        assert option.label == "Explorer"
        assert option.value == "3"
        assert option.description == "Cat"

    def test_no_family_leaves_description_unset(self) -> None:
        option = match_option(_beast(family=None))
        assert option.description is None


class TestAutocompleteLabel:
    def test_includes_family_when_present(self) -> None:
        assert autocomplete_label(_beast(name="Ashamane", family="Cat")) == "Ashamane (Cat)"

    def test_falls_back_to_name_without_family(self) -> None:
        assert autocomplete_label(_beast(name="Ashamane", family=None)) == "Ashamane"


class TestAutocompleteBeast:
    async def test_returns_choices_from_search(self) -> None:
        api = AsyncMock()
        api.search_beasts.return_value = [_beast(beast_id=1, name="Ashamane", family="Cat")]
        interaction = _make_interaction(api=api)

        choices = await _autocomplete_beast(interaction, "asha")

        assert [choice.value for choice in choices] == ["1"]
        assert choices[0].name == "Ashamane (Cat)"
        api.search_beasts.assert_awaited_once_with("asha", limit=25)

    async def test_empty_current_passes_no_query(self) -> None:
        api = AsyncMock()
        api.search_beasts.return_value = []
        interaction = _make_interaction(api=api)

        await _autocomplete_beast(interaction, "")

        api.search_beasts.assert_awaited_once_with(None, limit=25)

    async def test_api_error_returns_empty(self) -> None:
        api = AsyncMock()
        api.search_beasts.side_effect = APIError(503, "down")
        interaction = _make_interaction(api=api)

        assert await _autocomplete_beast(interaction, "asha") == []

    async def test_slow_catalog_build_times_out_to_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sidequest_bot.plugins.beasts.AUTOCOMPLETE_TIMEOUT", 0.01)

        async def _slow(*_args: object, **_kwargs: object) -> list[BeastSchema]:
            await asyncio.sleep(1)
            return [_beast()]

        api = AsyncMock()
        api.search_beasts.side_effect = _slow
        interaction = _make_interaction(api=api)

        choices = await _autocomplete_beast(interaction, "asha")

        assert choices == []


class TestAutocompleteBeastFamily:
    async def test_filters_by_substring(self) -> None:
        api = AsyncMock()
        api.list_beast_families.return_value = [_family(family_id=1, name="Cat"), _family(family_id=2, name="Bear")]
        interaction = _make_interaction(api=api)

        choices = await _autocomplete_beast_family(interaction, "cat")

        assert [choice.value for choice in choices] == ["1"]
        assert choices[0].name == "Cat (5)"

    async def test_empty_current_returns_everything(self) -> None:
        api = AsyncMock()
        api.list_beast_families.return_value = [_family(family_id=1, name="Cat")]
        interaction = _make_interaction(api=api)

        choices = await _autocomplete_beast_family(interaction, "")

        assert len(choices) == 1

    async def test_api_error_returns_empty(self) -> None:
        api = AsyncMock()
        api.list_beast_families.side_effect = APIError(503, "down")
        interaction = _make_interaction(api=api)

        assert await _autocomplete_beast_family(interaction, "cat") == []

    async def test_slow_catalog_build_times_out_to_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sidequest_bot.plugins.beasts.AUTOCOMPLETE_TIMEOUT", 0.01)

        async def _slow() -> list[BeastFamilySchema]:
            await asyncio.sleep(1)
            return [_family()]

        api = AsyncMock()
        api.list_beast_families.side_effect = _slow
        interaction = _make_interaction(api=api)

        choices = await _autocomplete_beast_family(interaction, "")

        assert choices == []


class TestBeastCommand:
    async def test_digit_name_fetches_directly(self) -> None:
        api = AsyncMock()
        api.get_beast.return_value = _beast(beast_id=42, name="Ashamane")
        cog = _make_cog(api=api)
        interaction = _make_interaction(api=api)

        await BeastsCog.beast_command.callback(cog, interaction, "42")

        api.get_beast.assert_awaited_once_with(42)
        kwargs = interaction.followup.send.await_args.kwargs
        assert kwargs["embed"].title == "Ashamane"
        labels = [button.label for button in kwargs["view"].children]
        assert labels == ["Beasts page", "Wowhead"]

    async def test_digit_name_not_found(self) -> None:
        api = AsyncMock()
        api.get_beast.side_effect = NotFoundError(404, "not tameable")
        cog = _make_cog(api=api)
        interaction = _make_interaction(api=api)

        await BeastsCog.beast_command.callback(cog, interaction, "42")

        interaction.followup.send.assert_awaited_once_with("That's not a tameable beast.", ephemeral=True)

    async def test_digit_name_unavailable(self) -> None:
        api = AsyncMock()
        api.get_beast.side_effect = APIError(503, "down")
        cog = _make_cog(api=api)
        interaction = _make_interaction(api=api)

        await BeastsCog.beast_command.callback(cog, interaction, "42")

        interaction.followup.send.assert_awaited_once_with(UNAVAILABLE_MESSAGE, ephemeral=True)

    async def test_name_search_unavailable(self) -> None:
        api = AsyncMock()
        api.search_beasts.side_effect = APIError(503, "down")
        cog = _make_cog(api=api)
        interaction = _make_interaction(api=api)

        await BeastsCog.beast_command.callback(cog, interaction, "ashamane")

        interaction.followup.send.assert_awaited_once_with(UNAVAILABLE_MESSAGE, ephemeral=True)

    async def test_name_search_generic_error(self) -> None:
        api = AsyncMock()
        api.search_beasts.side_effect = APIError(500, "boom")
        cog = _make_cog(api=api)
        interaction = _make_interaction(api=api)

        await BeastsCog.beast_command.callback(cog, interaction, "ashamane")

        message = interaction.followup.send.await_args.args[0]
        assert "try again shortly" in message

    async def test_no_matches_sends_hint(self) -> None:
        api = AsyncMock()
        api.search_beasts.return_value = []
        cog = _make_cog(api=api)
        interaction = _make_interaction(api=api)

        await BeastsCog.beast_command.callback(cog, interaction, "zzznotfound")

        message = interaction.followup.send.await_args.args[0]
        assert NOT_FOUND_HINT in message

    async def test_single_match_sends_link_buttons_without_select(self) -> None:
        api = AsyncMock()
        api.search_beasts.return_value = [_beast(beast_id=7, name="Ashamane")]
        api.get_beast.return_value = _beast(beast_id=7, name="Ashamane")
        cog = _make_cog(api=api)
        interaction = _make_interaction(api=api)

        await BeastsCog.beast_command.callback(cog, interaction, "ashamane")

        kwargs = interaction.followup.send.await_args.kwargs
        assert kwargs["embed"].title == "Ashamane"
        labels = [button.label for button in kwargs["view"].children]
        assert labels == ["Beasts page", "Wowhead"]

    async def test_multiple_matches_offers_select_of_others(self) -> None:
        api = AsyncMock()
        matches = [_beast(beast_id=i, name=f"Beast {i}") for i in range(1, 8)]
        api.search_beasts.return_value = matches
        api.get_beast.return_value = _beast(beast_id=1, name="Beast 1")
        cog = _make_cog(api=api)
        interaction = _make_interaction(api=api)

        await BeastsCog.beast_command.callback(cog, interaction, "beast")

        kwargs = interaction.followup.send.await_args.kwargs
        view = kwargs["view"]
        assert isinstance(view, BeastMatchView)
        assert [option.value for option in view._select.options] == ["2", "3", "4", "5", "6"]

    async def test_beast_gone_after_search_reports_not_tameable(self) -> None:
        api = AsyncMock()
        api.search_beasts.return_value = [_beast(beast_id=1, name="Ashamane")]
        api.get_beast.side_effect = NotFoundError(404, "not tameable")
        cog = _make_cog(api=api)
        interaction = _make_interaction(api=api)

        await BeastsCog.beast_command.callback(cog, interaction, "ashamane")

        interaction.followup.send.assert_awaited_once_with("That's not a tameable beast.", ephemeral=True)

    async def test_digit_family_filters_the_search(self) -> None:
        api = AsyncMock()
        api.search_beasts.return_value = [_beast(beast_id=1, name="Ashamane")]
        api.get_beast.return_value = _beast(beast_id=1, name="Ashamane")
        cog = _make_cog(api=api)
        interaction = _make_interaction(api=api)

        await BeastsCog.beast_command.callback(cog, interaction, "ashamane", "10")

        api.search_beasts.assert_awaited_once_with("ashamane", family_id=10, limit=25)

    async def test_non_digit_family_is_ignored(self) -> None:
        api = AsyncMock()
        api.search_beasts.return_value = [_beast(beast_id=1, name="Ashamane")]
        api.get_beast.return_value = _beast(beast_id=1, name="Ashamane")
        cog = _make_cog(api=api)
        interaction = _make_interaction(api=api)

        await BeastsCog.beast_command.callback(cog, interaction, "ashamane", "not-an-id")

        api.search_beasts.assert_awaited_once_with("ashamane", family_id=None, limit=25)


class TestBeastMatchView:
    def test_options_use_match_names_and_ids(self) -> None:
        view = BeastMatchView(1, [_beast(beast_id=1, name="A"), _beast(beast_id=2, name="B")])
        assert [option.label for option in view._select.options] == ["A", "B"]
        assert [option.value for option in view._select.options] == ["1", "2"]

    def test_link_buttons_point_at_initial_beast(self) -> None:
        view = BeastMatchView(1, [_beast(beast_id=2, name="B")])
        assert view._page_button.url == "https://sidequest-guild.com/beasts/"
        assert view._wowhead_button.url == "https://www.wowhead.com/forever/npc=1"

    async def test_select_callback_edits_message_with_chosen_beast(self) -> None:
        api = AsyncMock()
        api.get_beast.return_value = _beast(beast_id=2, name="B")
        view = BeastMatchView(1, [_beast(beast_id=2, name="B")])
        view._select._values = ["2"]
        interaction = _make_interaction(api=api)

        await view._select.callback(interaction)

        interaction.response.edit_message.assert_awaited_once()
        kwargs = interaction.response.edit_message.await_args.kwargs
        assert kwargs["embed"].title == "B"
        assert kwargs["view"] is view
        assert view._page_button.url == "https://sidequest-guild.com/beasts/"
        assert view._wowhead_button.url == "https://www.wowhead.com/forever/npc=2"

    async def test_select_callback_reports_error_ephemerally(self) -> None:
        api = AsyncMock()
        api.get_beast.side_effect = NotFoundError(404, "not tameable")
        view = BeastMatchView(1, [_beast(beast_id=2)])
        view._select._values = ["2"]
        interaction = _make_interaction(api=api)

        await view._select.callback(interaction)

        interaction.response.send_message.assert_awaited_once_with("That's not a tameable beast.", ephemeral=True)
        interaction.response.edit_message.assert_not_awaited()
