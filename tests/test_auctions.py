"""Unit tests for the auctions plugin: money parsing/formatting, embeds, the hourly sync loop, alert DMs and
``/price``, ``/watch``, ``/unwatch``, ``/watches``.
"""

from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock
from uuid import uuid4

import discord

from sidequest_bot.api_client import (
    APIError,
    AuctionAlertSchema,
    AuctionHousePrice,
    AuctionWatch,
    Item,
    ItemAuctionPrice,
    ItemSummary,
    NotFoundError,
)
from sidequest_bot.plugins.auctions import (
    NOT_LISTED_MESSAGE,
    UNAVAILABLE_MESSAGE,
    AuctionsCog,
    _autocomplete_item,
    build_alert_embed,
    build_price_embed,
    format_price,
    item_choice,
    parse_price,
    watch_line,
)


def _item(*, item_id: int = 19019, name: str = "Thunderfury", **overrides: object) -> Item:
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
        "tooltip": ["Binds when picked up"],
    }
    base.update(overrides)
    return Item(**base)


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


def _house(
    *, house_id: int = 110, house_name: str = "Alliance Auction House", unit_price: int = 1000, **overrides: object
) -> AuctionHousePrice:
    base: dict[str, object] = {
        "house_id": house_id,
        "house_name": house_name,
        "unit_price": unit_price,
        "quantity": 5,
        "listings": 2,
        "updated_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    base.update(overrides)
    return AuctionHousePrice(**base)


def _price(**overrides: object) -> ItemAuctionPrice:
    base: dict[str, object] = {
        "item_id": 19019,
        "unit_price": 1000,
        "quantity_total": 5,
        "houses": [_house()],
        "trend": [],
        "updated_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    base.update(overrides)
    return ItemAuctionPrice(**base)


def _alert(**overrides: object) -> AuctionAlertSchema:
    base: dict[str, object] = {
        "id": uuid4(),
        "discord_user_id": 555,
        "member_name": "Nox",
        "item_id": 19019,
        "item_name": "Thunderfury",
        "icon_url": None,
        "unit_price": 900,
        "max_unit_price": 1000,
        "quantity": 3,
        "house_name": "Alliance Auction House",
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    base.update(overrides)
    return AuctionAlertSchema(**base)


def _watch(**overrides: object) -> AuctionWatch:
    base: dict[str, object] = {
        "id": uuid4(),
        "item_id": 19019,
        "item_name": "Thunderfury",
        "icon_url": None,
        "max_unit_price": 1000,
        "current_unit_price": 900,
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "last_alerted_at": None,
    }
    base.update(overrides)
    return AuctionWatch(**base)


def _fake_guild(guild_id: int = 42, *, member: object | None = None) -> MagicMock:
    guild = MagicMock(spec=discord.Guild)
    guild.id = guild_id
    guild.get_member.return_value = member
    return guild


def _make_cog(*, api: object | None = None, guilds: list[object] | None = None) -> AuctionsCog:
    cog = AuctionsCog.__new__(AuctionsCog)
    cog.bot = SimpleNamespace(
        api=api or AsyncMock(), guilds=guilds or [], get_user=Mock(return_value=None), fetch_user=AsyncMock()
    )
    cog._synced_hour = {}
    return cog


def _make_interaction(*, api: object | None = None, guild_id: int = 1) -> Mock:
    interaction = Mock(spec=discord.Interaction)
    interaction.guild_id = guild_id
    interaction.user = MagicMock(spec=discord.Member)
    interaction.user.id = 777
    interaction.response = Mock()
    interaction.response.defer = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.followup = Mock()
    interaction.followup.send = AsyncMock()
    interaction.client = SimpleNamespace(api=api or AsyncMock())
    return interaction


class TestParsePrice:
    def test_gold_silver_copper(self) -> None:
        assert parse_price("1g20s") == 12000

    def test_silver_only(self) -> None:
        assert parse_price("35s") == 3500

    def test_copper_only(self) -> None:
        assert parse_price("80c") == 80

    def test_plain_decimal_gold(self) -> None:
        assert parse_price("1.5") == 15000

    def test_with_spaces(self) -> None:
        assert parse_price("1g 20s") == 12000

    def test_empty_is_none(self) -> None:
        assert parse_price("") is None

    def test_garbage_is_none(self) -> None:
        assert parse_price("free") is None

    def test_all_zero_is_none(self) -> None:
        assert parse_price("0g0s0c") is None


class TestFormatPrice:
    def test_splits_and_omits_zero_parts(self) -> None:
        assert format_price(12000) == "1g 20s"

    def test_all_parts(self) -> None:
        assert format_price(123456) == "12g 34s 56c"

    def test_zero_shows_zero_copper(self) -> None:
        assert format_price(0) == "0c"

    def test_copper_only(self) -> None:
        assert format_price(5) == "5c"


class TestBuildAlertEmbed:
    def test_title_price_quantity_and_house(self) -> None:
        embed = build_alert_embed(_alert())
        assert embed.title == "Thunderfury is on sale"
        assert "sidequest-guild.com/items/detail/?id=19019" in (embed.description or "")
        field_values = {f.name: f.value for f in embed.fields}
        assert field_values["Price"] == "9s (your max: 10s)"
        assert field_values["Quantity"] == "3"
        assert field_values["House"] == "Alliance Auction House"

    def test_missing_item_name_falls_back_to_id(self) -> None:
        embed = build_alert_embed(_alert(item_name=None))
        assert embed.title == "Item 19019 is on sale"


class TestBuildPriceEmbed:
    def test_unlisted_item_shows_not_listed_message(self) -> None:
        embed = build_price_embed(_item(), _price(unit_price=None, houses=[], trend=[]))
        assert embed.description == NOT_LISTED_MESSAGE
        assert embed.fields == []

    def test_lists_lowest_price_house_and_total(self) -> None:
        embed = build_price_embed(_item(), _price(unit_price=1000, houses=[_house(unit_price=1000)]))
        field_values = {f.name: f.value for f in embed.fields}
        assert field_values["Lowest price"] == "10s at Alliance Auction House"
        assert field_values["Total listed"] == "5"

    def test_includes_per_house_breakdown(self) -> None:
        houses = [
            _house(house_id=110, house_name="Alliance Auction House", unit_price=1000),
            _house(house_id=111, house_name="Horde Auction House", unit_price=500),
        ]
        embed = build_price_embed(_item(), _price(houses=houses))
        field_values = {f.name: f.value for f in embed.fields}
        assert "Horde Auction House: 5s" in field_values["By house"]
        assert "Alliance Auction House: 10s" in field_values["By house"]

    def test_includes_trend_low_and_high(self) -> None:
        trend = [
            {"day": date(2026, 1, 1), "unit_price": 500},
            {"day": date(2026, 1, 2), "unit_price": 1500},
        ]
        embed = build_price_embed(_item(), _price(trend=trend))
        field_values = {f.name: f.value for f in embed.fields}
        assert field_values["14-day range"] == "5s - 15s"


class TestItemChoice:
    def test_name_and_item_level_in_label(self) -> None:
        choice = item_choice(_summary(item_id=5, name="Sword", item_level=70))
        assert choice.name == "Sword (ilvl 70)"
        assert choice.value == 5


class TestWatchLine:
    def test_known_current_price(self) -> None:
        line = watch_line(_watch(item_name="Thunderfury", current_unit_price=900, max_unit_price=1000))
        assert line == "**Thunderfury** — currently 9s, alert below 10s"

    def test_unlisted_current_price(self) -> None:
        line = watch_line(_watch(current_unit_price=None))
        assert "unlisted" in line


class TestSyncLoop:
    async def test_before_sync_minute_does_nothing(self) -> None:
        guild = _fake_guild()
        api = AsyncMock()
        cog = _make_cog(api=api, guilds=[guild])

        with _patch_utcnow(minute=10):
            await cog._sync_loop.coro(cog)

        api.sync_auctions.assert_not_awaited()

    async def test_at_sync_minute_syncs_each_guild_once(self) -> None:
        guilds = [_fake_guild(1), _fake_guild(2)]
        api = AsyncMock()
        api.sync_auctions.return_value = SimpleNamespace(houses=[], items_priced=0, alerts_queued=0)
        api.pending_auction_alerts.return_value = []
        cog = _make_cog(api=api, guilds=guilds)

        with _patch_utcnow(minute=40):
            await cog._sync_loop.coro(cog)

        assert api.sync_auctions.await_count == 2

    async def test_same_hour_is_not_synced_twice(self) -> None:
        guild = _fake_guild()
        api = AsyncMock()
        api.sync_auctions.return_value = SimpleNamespace(houses=[], items_priced=0, alerts_queued=0)
        api.pending_auction_alerts.return_value = []
        cog = _make_cog(api=api, guilds=[guild])

        with _patch_utcnow(minute=40):
            await cog._sync_loop.coro(cog)
            await cog._sync_loop.coro(cog)

        assert api.sync_auctions.await_count == 1

    async def test_not_found_does_not_raise(self) -> None:
        guild = _fake_guild()
        api = AsyncMock()
        api.sync_auctions.side_effect = NotFoundError(404, "not deployed yet")
        cog = _make_cog(api=api, guilds=[guild])

        with _patch_utcnow(minute=40):
            await cog._sync_loop.coro(cog)  # must not raise

    async def test_service_unavailable_does_not_raise(self) -> None:
        guild = _fake_guild()
        api = AsyncMock()
        api.sync_auctions.side_effect = APIError(503, "Battle.net unconfigured")
        cog = _make_cog(api=api, guilds=[guild])

        with _patch_utcnow(minute=40):
            await cog._sync_loop.coro(cog)  # must not raise

    async def test_other_api_error_does_not_raise(self) -> None:
        guild = _fake_guild()
        api = AsyncMock()
        api.sync_auctions.side_effect = APIError(500, "boom")
        cog = _make_cog(api=api, guilds=[guild])

        with _patch_utcnow(minute=40):
            await cog._sync_loop.coro(cog)  # must not raise


def _patch_utcnow(*, minute: int):
    from contextlib import contextmanager
    from unittest.mock import patch

    @contextmanager
    def _ctx():
        with patch("sidequest_bot.plugins.auctions.discord.utils.utcnow") as mock_utcnow:
            mock_utcnow.return_value = datetime(2026, 11, 6, 14, minute, tzinfo=UTC)
            yield

    return _ctx()


class TestSendAlert:
    async def test_dms_member_and_acks(self) -> None:
        member = MagicMock(spec=discord.Member)
        member.send = AsyncMock()
        guild = _fake_guild(member=member)
        api = AsyncMock()
        cog = _make_cog(api=api)
        alert = _alert()

        await cog._send_alert(guild, alert)

        member.send.assert_awaited_once()
        api.ack_auction_alert.assert_awaited_once_with(guild.id, alert.id)

    async def test_forbidden_dm_is_still_acked(self) -> None:
        member = MagicMock(spec=discord.Member)
        member.send = AsyncMock(side_effect=discord.Forbidden(MagicMock(), "closed"))
        guild = _fake_guild(member=member)
        api = AsyncMock()
        cog = _make_cog(api=api)
        alert = _alert()

        await cog._send_alert(guild, alert)  # must not raise

        api.ack_auction_alert.assert_awaited_once_with(guild.id, alert.id)

    async def test_http_exception_on_dm_is_still_acked(self) -> None:
        member = MagicMock(spec=discord.Member)
        member.send = AsyncMock(side_effect=discord.HTTPException(MagicMock(), "boom"))
        guild = _fake_guild(member=member)
        api = AsyncMock()
        cog = _make_cog(api=api)
        alert = _alert()

        await cog._send_alert(guild, alert)  # must not raise

        api.ack_auction_alert.assert_awaited_once_with(guild.id, alert.id)

    async def test_unresolvable_user_is_still_acked(self) -> None:
        guild = _fake_guild(member=None)
        api = AsyncMock()
        cog = _make_cog(api=api)
        cog.bot.get_user = Mock(return_value=None)
        cog.bot.fetch_user = AsyncMock(side_effect=discord.NotFound(MagicMock(), "unknown"))
        alert = _alert()

        await cog._send_alert(guild, alert)  # must not raise

        api.ack_auction_alert.assert_awaited_once_with(guild.id, alert.id)

    async def test_ack_failure_does_not_raise(self) -> None:
        member = MagicMock(spec=discord.Member)
        member.send = AsyncMock()
        guild = _fake_guild(member=member)
        api = AsyncMock()
        api.ack_auction_alert.side_effect = APIError(500, "boom")
        cog = _make_cog(api=api)

        await cog._send_alert(guild, _alert())  # must not raise


class TestSendPendingAlerts:
    async def test_not_found_does_not_raise(self) -> None:
        guild = _fake_guild()
        api = AsyncMock()
        api.pending_auction_alerts.side_effect = NotFoundError(404, "not deployed yet")
        cog = _make_cog(api=api)

        await cog._send_pending_alerts(guild)  # must not raise

    async def test_sends_each_pending_alert(self) -> None:
        member = MagicMock(spec=discord.Member)
        member.send = AsyncMock()
        guild = _fake_guild(member=member)
        api = AsyncMock()
        api.pending_auction_alerts.return_value = [_alert(), _alert()]
        cog = _make_cog(api=api)

        await cog._send_pending_alerts(guild)

        assert member.send.await_count == 2
        assert api.ack_auction_alert.await_count == 2


class TestPriceCommand:
    async def test_item_gone_reports_item_gone(self) -> None:
        api = AsyncMock()
        api.get_item.side_effect = NotFoundError(404, "gone")
        cog = _make_cog(api=api)
        interaction = _make_interaction(api=api)

        await AuctionsCog.price_command.callback(cog, interaction, 19019)

        interaction.followup.send.assert_awaited_once_with("That item is gone.", ephemeral=True)

    async def test_unavailable_reports_unavailable_message(self) -> None:
        api = AsyncMock()
        api.get_item.side_effect = APIError(503, "Battle.net unavailable")
        cog = _make_cog(api=api)
        interaction = _make_interaction(api=api)

        await AuctionsCog.price_command.callback(cog, interaction, 19019)

        interaction.followup.send.assert_awaited_once_with(UNAVAILABLE_MESSAGE, ephemeral=True)

    async def test_success_sends_price_embed(self) -> None:
        api = AsyncMock()
        api.get_item.return_value = _item()
        api.get_item_price.return_value = _price()
        cog = _make_cog(api=api)
        interaction = _make_interaction(api=api)

        await AuctionsCog.price_command.callback(cog, interaction, 19019)

        kwargs = interaction.followup.send.await_args.kwargs
        assert kwargs["embed"].title == "Thunderfury"
        assert kwargs["ephemeral"] is True


class TestWatchCommand:
    async def test_unparseable_price_is_rejected_before_any_call(self) -> None:
        api = AsyncMock()
        cog = _make_cog(api=api)
        interaction = _make_interaction(api=api)

        await AuctionsCog.watch_command.callback(cog, interaction, 19019, "free")

        interaction.response.send_message.assert_awaited_once()
        assert "Couldn't understand that price" in interaction.response.send_message.await_args.args[0]
        api.upsert_auction_watch.assert_not_awaited()

    async def test_not_linked_reports_detail(self) -> None:
        api = AsyncMock()
        api.upsert_auction_watch.side_effect = NotFoundError(404, "link your Discord account on the site")
        cog = _make_cog(api=api)
        interaction = _make_interaction(api=api)

        await AuctionsCog.watch_command.callback(cog, interaction, 19019, "1g")

        interaction.followup.send.assert_awaited_once_with("link your Discord account on the site", ephemeral=True)

    async def test_conflict_reports_watch_limit(self) -> None:
        api = AsyncMock()
        api.upsert_auction_watch.side_effect = APIError(409, "limit")
        cog = _make_cog(api=api)
        interaction = _make_interaction(api=api)

        await AuctionsCog.watch_command.callback(cog, interaction, 19019, "1g")

        message = interaction.followup.send.await_args.args[0]
        assert "25 items" in message

    async def test_success_confirms_watch(self) -> None:
        api = AsyncMock()
        api.upsert_auction_watch.return_value = _watch(item_name="Thunderfury", max_unit_price=12000)
        cog = _make_cog(api=api)
        interaction = _make_interaction(api=api)

        await AuctionsCog.watch_command.callback(cog, interaction, 19019, "1g20s")

        api.upsert_auction_watch.assert_awaited_once()
        message = interaction.followup.send.await_args.args[0]
        assert "Thunderfury" in message
        assert "1g 20s" in message


class TestUnwatchCommand:
    async def test_not_linked_reports_detail(self) -> None:
        api = AsyncMock()
        api.delete_auction_watch.side_effect = NotFoundError(404, "link your Discord account on the site")
        cog = _make_cog(api=api)
        interaction = _make_interaction(api=api)

        await AuctionsCog.unwatch_command.callback(cog, interaction, 19019)

        interaction.followup.send.assert_awaited_once_with("link your Discord account on the site", ephemeral=True)

    async def test_success_confirms_removal(self) -> None:
        api = AsyncMock()
        cog = _make_cog(api=api)
        interaction = _make_interaction(api=api)

        await AuctionsCog.unwatch_command.callback(cog, interaction, 19019)

        api.delete_auction_watch.assert_awaited_once_with(interaction.guild_id, interaction.user.id, 19019)
        interaction.followup.send.assert_awaited_once_with("Watch removed.", ephemeral=True)


class TestWatchesCommand:
    async def test_no_watches_prompts_to_add_one(self) -> None:
        api = AsyncMock()
        api.list_auction_watches.return_value = []
        cog = _make_cog(api=api)
        interaction = _make_interaction(api=api)

        await AuctionsCog.watches_command.callback(cog, interaction)

        message = interaction.followup.send.await_args.args[0]
        assert "/watch" in message

    async def test_lists_each_watch(self) -> None:
        api = AsyncMock()
        api.list_auction_watches.return_value = [_watch(item_name="A"), _watch(item_name="B")]
        cog = _make_cog(api=api)
        interaction = _make_interaction(api=api)

        await AuctionsCog.watches_command.callback(cog, interaction)

        kwargs = interaction.followup.send.await_args.kwargs
        assert "A" in kwargs["embed"].description
        assert "B" in kwargs["embed"].description


class TestAutocompleteItem:
    async def test_short_text_returns_empty(self) -> None:
        api = AsyncMock()
        interaction = _make_interaction(api=api)

        choices = await _autocomplete_item(interaction, "a")

        assert choices == []
        api.search_items.assert_not_awaited()

    async def test_searches_and_returns_choices(self) -> None:
        api = AsyncMock()
        api.search_items.return_value = SimpleNamespace(results=[_summary(item_id=5, name="Sword")])
        interaction = _make_interaction(api=api)

        choices = await _autocomplete_item(interaction, "sw")

        assert choices[0].value == 5

    async def test_api_error_returns_empty(self) -> None:
        api = AsyncMock()
        api.search_items.side_effect = APIError(503, "down")
        interaction = _make_interaction(api=api)

        assert await _autocomplete_item(interaction, "sw") == []


class TestResolveUser:
    async def test_prefers_cached_guild_member(self) -> None:
        member = MagicMock(spec=discord.Member)
        guild = _fake_guild(member=member)
        cog = _make_cog()

        assert await cog._resolve_user(guild, 555) is member

    async def test_falls_back_to_cached_user(self) -> None:
        guild = _fake_guild(member=None)
        user = MagicMock(spec=discord.User)
        cog = _make_cog()
        cog.bot.get_user = Mock(return_value=user)

        assert await cog._resolve_user(guild, 555) is user

    async def test_falls_back_to_fetch_user(self) -> None:
        guild = _fake_guild(member=None)
        user = MagicMock(spec=discord.User)
        cog = _make_cog()
        cog.bot.get_user = Mock(return_value=None)
        cog.bot.fetch_user = AsyncMock(return_value=user)

        assert await cog._resolve_user(guild, 555) is user

    async def test_not_found_returns_none(self) -> None:
        guild = _fake_guild(member=None)
        cog = _make_cog()
        cog.bot.get_user = Mock(return_value=None)
        cog.bot.fetch_user = AsyncMock(side_effect=discord.NotFound(MagicMock(), "x"))

        assert await cog._resolve_user(guild, 555) is None
