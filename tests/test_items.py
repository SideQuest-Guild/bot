"""Unit tests for the items plugin: the embed builder, ``/item``, the match select menu, and link unfurling."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord

from sidequest_bot.api_client import APIError, Item, ItemSummary, NotFoundError
from sidequest_bot.plugins.items import (
    NOT_FOUND_HINT,
    UNAVAILABLE_MESSAGE,
    ItemMatchView,
    ItemsCog,
    build_item_embed,
    format_sell_price,
    item_quality_color,
)


def _summary(*, item_id: int = 1, name: str = "Example Item", **overrides: object) -> ItemSummary:
    base: dict[str, object] = {
        "item_id": item_id,
        "name": name,
        "quality": "RARE",
        "quality_name": "Rare",
        "inventory_type": "TWOHWEAPON",
        "inventory_type_name": "Two-Hand",
        "item_level": 70,
        "required_level": 60,
    }
    base.update(overrides)
    return ItemSummary(**base)


def _item(
    *, item_id: int = 19019, name: str = "Thunderfury, Blessed Blade of the Windseeker", **overrides: object
) -> Item:
    base: dict[str, object] = {
        "item_id": item_id,
        "name": name,
        "quality": "LEGENDARY",
        "quality_name": "Legendary",
        "item_class": "Weapon",
        "item_subclass": "Sword",
        "inventory_type": "ONEHWEAPON",
        "inventory_type_name": "One-Hand",
        "item_level": 80,
        "required_level": 60,
        "sell_price": 123456,
        "is_equippable": True,
        "icon_url": "https://render.worldofwarcraft.com/icons/56/inv_sword_39.jpg",
        "tooltip": ["Binds when picked up", "Unique", "One-Hand Sword"],
    }
    base.update(overrides)
    return Item(**base)


def _make_cog(api: object) -> ItemsCog:
    cog = ItemsCog.__new__(ItemsCog)
    cog.bot = SimpleNamespace(api=api)
    return cog


def _make_interaction(api: object) -> Mock:
    interaction = Mock(spec=discord.Interaction)
    interaction.response = Mock()
    interaction.response.defer = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    interaction.followup = Mock()
    interaction.followup.send = AsyncMock()
    interaction.client = SimpleNamespace(api=api)
    return interaction


def _make_message(content: str, *, author_bot: bool = False) -> SimpleNamespace:
    channel = SimpleNamespace(id=10, send=AsyncMock())
    return SimpleNamespace(author=SimpleNamespace(bot=author_bot), content=content, channel=channel)


class TestItemQualityColor:
    def test_known_qualities_match_wow_colors(self) -> None:
        assert item_quality_color("EPIC") == discord.Colour(0xA335EE)
        assert item_quality_color("LEGENDARY") == discord.Colour(0xFF8000)
        assert item_quality_color("POOR") == discord.Colour(0x9D9D9D)

    def test_unknown_quality_falls_back_to_house_slate(self) -> None:
        from sidequest_bot.lib.embeds import Colors

        assert item_quality_color("ARTIFACT") == Colors.SLATE


class TestFormatSellPrice:
    def test_splits_into_gold_silver_copper(self) -> None:
        assert format_sell_price(123456) == "12g 34s 56c"

    def test_zero_is_all_zeros(self) -> None:
        assert format_sell_price(0) == "0g 0s 0c"

    def test_under_a_silver(self) -> None:
        assert format_sell_price(7) == "0g 0s 7c"


class TestBuildItemEmbed:
    def test_sets_title_color_and_thumbnail(self) -> None:
        embed = build_item_embed(_item())
        assert embed.title == "Thunderfury, Blessed Blade of the Windseeker"
        assert embed.color == discord.Colour(0xFF8000)
        assert embed.thumbnail.url == "https://render.worldofwarcraft.com/icons/56/inv_sword_39.jpg"

    def test_description_includes_tooltip_lines_and_sell_price(self) -> None:
        embed = build_item_embed(_item())
        assert embed.description is not None
        assert "Binds when picked up" in embed.description
        assert "Unique" in embed.description
        assert "Sells for 12g 34s 56c." in embed.description

    def test_links_field_has_site_and_wowhead(self) -> None:
        embed = build_item_embed(_item(item_id=19019))
        assert len(embed.fields) == 1
        value = embed.fields[0].value
        assert value is not None
        assert "https://sidequest-guild.com/items/detail/?id=19019" in value
        assert "https://www.wowhead.com/forever/item=19019" in value

    def test_no_icon_leaves_thumbnail_unset(self) -> None:
        embed = build_item_embed(_item(icon_url=None))
        assert embed.thumbnail.url is None


class TestItemCommand:
    async def test_no_results_sends_hint_ephemeral(self) -> None:
        api = AsyncMock()
        api.search_items.return_value = SimpleNamespace(results=[])
        cog = _make_cog(api)
        interaction = _make_interaction(api)

        await ItemsCog.item_command.callback(cog, interaction, "zzzznotanitem")

        interaction.followup.send.assert_awaited_once()
        message, kwargs = interaction.followup.send.await_args.args, interaction.followup.send.await_args.kwargs
        assert NOT_FOUND_HINT in message[0]
        assert kwargs["ephemeral"] is True

    async def test_single_result_sends_embed_without_select(self) -> None:
        api = AsyncMock()
        api.search_items.return_value = SimpleNamespace(results=[_summary(item_id=19019, name="Thunderfury")])
        api.get_item.return_value = _item(item_id=19019, name="Thunderfury")
        cog = _make_cog(api)
        interaction = _make_interaction(api)

        await ItemsCog.item_command.callback(cog, interaction, "thunderfury")

        interaction.followup.send.assert_awaited_once()
        kwargs = interaction.followup.send.await_args.kwargs
        assert kwargs["embed"].title == "Thunderfury"
        assert "view" not in kwargs

    async def test_multiple_results_offers_select_of_others(self) -> None:
        api = AsyncMock()
        matches = [_summary(item_id=i, name=f"Item {i}") for i in range(1, 8)]
        api.search_items.return_value = SimpleNamespace(results=matches)
        api.get_item.return_value = _item(item_id=1, name="Item 1")
        cog = _make_cog(api)
        interaction = _make_interaction(api)

        await ItemsCog.item_command.callback(cog, interaction, "item")

        kwargs = interaction.followup.send.await_args.kwargs
        view = kwargs["view"]
        assert isinstance(view, ItemMatchView)
        assert [option.value for option in view._select.options] == ["2", "3", "4", "5", "6"]

    async def test_search_unavailable_reports_clear_message(self) -> None:
        api = AsyncMock()
        api.search_items.side_effect = APIError(503, "Battle.net unavailable")
        cog = _make_cog(api)
        interaction = _make_interaction(api)

        await ItemsCog.item_command.callback(cog, interaction, "bless")

        interaction.followup.send.assert_awaited_once_with(UNAVAILABLE_MESSAGE, ephemeral=True)

    async def test_search_generic_error_reports_try_again(self) -> None:
        api = AsyncMock()
        api.search_items.side_effect = APIError(500, "boom")
        cog = _make_cog(api)
        interaction = _make_interaction(api)

        await ItemsCog.item_command.callback(cog, interaction, "bless")

        message = interaction.followup.send.await_args.args[0]
        assert "try again shortly" in message

    async def test_item_gone_after_search_reports_item_gone(self) -> None:
        api = AsyncMock()
        api.search_items.return_value = SimpleNamespace(results=[_summary(item_id=1)])
        api.get_item.side_effect = NotFoundError(404, "gone")
        cog = _make_cog(api)
        interaction = _make_interaction(api)

        await ItemsCog.item_command.callback(cog, interaction, "example")

        interaction.followup.send.assert_awaited_once_with("That item is gone.", ephemeral=True)


class TestItemMatchView:
    def test_options_use_match_names_and_ids(self) -> None:
        view = ItemMatchView([_summary(item_id=1, name="Sword of A"), _summary(item_id=2, name="Sword of B")])
        assert [o.label for o in view._select.options] == ["Sword of A", "Sword of B"]
        assert [o.value for o in view._select.options] == ["1", "2"]

    async def test_select_callback_edits_message_with_chosen_item(self) -> None:
        api = AsyncMock()
        api.get_item.return_value = _item(item_id=2, name="Sword of B")
        view = ItemMatchView([_summary(item_id=2, name="Sword of B")])
        view._select._values = ["2"]
        interaction = _make_interaction(api)

        await view._select.callback(interaction)

        interaction.response.edit_message.assert_awaited_once()
        kwargs = interaction.response.edit_message.await_args.kwargs
        assert kwargs["embed"].title == "Sword of B"
        assert kwargs["view"] is view

    async def test_select_callback_reports_error_ephemerally(self) -> None:
        api = AsyncMock()
        api.get_item.side_effect = NotFoundError(404, "gone")
        view = ItemMatchView([_summary(item_id=2)])
        view._select._values = ["2"]
        interaction = _make_interaction(api)

        await view._select.callback(interaction)

        interaction.response.send_message.assert_awaited_once_with("That item is gone.", ephemeral=True)
        interaction.response.edit_message.assert_not_awaited()


class TestOnMessageUnfurl:
    async def test_bot_messages_are_ignored(self) -> None:
        api = AsyncMock()
        cog = _make_cog(api)
        message = _make_message("https://sidequest-guild.com/items/detail/?id=19019", author_bot=True)

        await cog.on_message(message)

        api.get_item.assert_not_called()

    async def test_messages_without_item_links_are_ignored(self) -> None:
        api = AsyncMock()
        cog = _make_cog(api)
        message = _make_message("just chatting")

        await cog.on_message(message)

        api.get_item.assert_not_called()

    async def test_item_link_posts_its_embed(self) -> None:
        api = AsyncMock()
        api.get_item.return_value = _item(item_id=19019, name="Thunderfury")
        cog = _make_cog(api)
        message = _make_message("check this out: https://sidequest-guild.com/items/detail/?id=19019")

        await cog.on_message(message)

        api.get_item.assert_awaited_once_with(19019)
        message.channel.send.assert_awaited_once()
        assert message.channel.send.await_args.kwargs["embed"].title == "Thunderfury"

    async def test_unknown_item_link_is_skipped_silently(self) -> None:
        api = AsyncMock()
        api.get_item.side_effect = NotFoundError(404, "gone")
        cog = _make_cog(api)
        message = _make_message("https://sidequest-guild.com/items/detail/?id=999999")

        await cog.on_message(message)

        message.channel.send.assert_not_awaited()

    async def test_only_first_three_distinct_links_are_unfurled(self) -> None:
        api = AsyncMock()
        api.get_item.return_value = _item()
        cog = _make_cog(api)
        urls = " ".join(f"https://sidequest-guild.com/items/detail/?id={i}" for i in range(1, 6))
        message = _make_message(urls)

        await cog.on_message(message)

        assert api.get_item.await_count == 3
        assert {call.args[0] for call in api.get_item.await_args_list} == {1, 2, 3}
