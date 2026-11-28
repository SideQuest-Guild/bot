"""Unit tests for the market-report plugin: timing, level-band selection, money/line formatting, Discord length
limits, and the daily check loop.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from sidequest_bot.api_client import APIError, NotFoundError
from sidequest_bot.plugins.market_report import (
    GATHER_PROFESSIONS,
    GUILD_TZ,
    MarketReportCog,
    band_for_level,
    band_picks,
    build_report_embeds,
    build_report_view,
    craft_pick_lines,
    data_age,
    format_change,
    format_cooldown,
    format_money,
    format_sold_per_day,
    gather_pick_lines,
    should_post,
)
from sidequest_common.schemas import (
    MarketCraftPick,
    MarketCraftSection,
    MarketGatherPick,
    MarketGatherSection,
    MarketItemRef,
    MarketReport,
    MarketZone,
)

BASE_URL = "https://sidequest-guild.com"


def _item(*, item_id: int = 1, name: str = "Kingsblood") -> MarketItemRef:
    return MarketItemRef(item_id=item_id, name=name, quality=None)


def _zone(name: str, min_level: int, max_level: int) -> MarketZone:
    return MarketZone(name=name, min_level=min_level, max_level=max_level)


def _gather_pick(**overrides: object) -> MarketGatherPick:
    base: dict[str, object] = {
        "item": _item(),
        "price": 4800,
        "stack_size": 20,
        "stack_price": 96000,
        "sold_per_day": 210.0,
        "change_pct": 12.0,
        "skill": 125,
        "min_level": 10,
        "max_level": 30,
        "zones": [_zone("Arathi Highlands", 10, 30), _zone("Stranglethorn Vale", 20, 35)],
        "trend": [],
    }
    base.update(overrides)
    return MarketGatherPick(**base)


def _craft_pick(**overrides: object) -> MarketCraftPick:
    base: dict[str, object] = {
        "recipe_id": 1,
        "recipe_name": "Recipe: Major Healing Potion",
        "item": _item(item_id=2, name="Major Healing Potion"),
        "makes": 1.0,
        "price": 68000,
        "mats_cost": 23000,
        "profit": 41000,
        "margin_pct": 178.0,
        "sold_per_day": 35.0,
        "change_pct": None,
        "reagents": [],
        "trend": [],
    }
    base.update(overrides)
    return MarketCraftPick(**base)


def _report(**overrides: object) -> MarketReport:
    base: dict[str, object] = {
        "kind": "daily",
        "house_name": "Stormwind",
        "data_at": datetime(2026, 11, 20, 14, 0, tzinfo=UTC),
        "stale": False,
        "period_start": datetime(2026, 11, 13).date(),
        "period_end": datetime(2026, 11, 20).date(),
        "gathering": [MarketGatherSection(profession="Herbalism", picks=[_gather_pick()])],
        "crafting": [MarketCraftSection(profession="Alchemy", picks=[_craft_pick()])],
        "risers": [],
        "fallers": [],
    }
    base.update(overrides)
    return MarketReport(**base)


class TestShouldPost:
    def test_before_post_time_returns_none(self) -> None:
        now = datetime(2026, 11, 20, 15, 30, tzinfo=UTC)  # 09:30 Central
        assert should_post(now, last_day=None) is None

    def test_at_or_after_post_time_returns_todays_central_date(self) -> None:
        now = datetime(2026, 11, 20, 16, 0, tzinfo=UTC)  # 10:00 Central
        assert should_post(now, last_day=None) == "2026-11-20"

    def test_already_posted_today_returns_none(self) -> None:
        now = datetime(2026, 11, 20, 18, 0, tzinfo=UTC)  # 12:00 Central
        assert should_post(now, last_day="2026-11-20") is None

    def test_a_new_day_posts_again(self) -> None:
        now = datetime(2026, 11, 21, 16, 0, tzinfo=UTC)
        assert should_post(now, last_day="2026-11-20") == "2026-11-21"


class TestBandForLevel:
    def test_bands_by_min_level(self) -> None:
        assert band_for_level(1) == "1-24"
        assert band_for_level(24) == "1-24"
        assert band_for_level(25) == "25-44"
        assert band_for_level(44) == "25-44"
        assert band_for_level(45) == "45-60"
        assert band_for_level(60) == "45-60"


class TestBandPicks:
    def test_one_pick_per_band_best_first_lowest_band_first(self) -> None:
        low_best = _gather_pick(item=_item(name="Low best"), min_level=1)
        low_worst = _gather_pick(item=_item(name="Low worst"), min_level=10)
        mid = _gather_pick(item=_item(name="Mid"), min_level=30)
        high = _gather_pick(item=_item(name="High"), min_level=50)
        picks = [low_best, low_worst, mid, high]

        selected = band_picks(picks)

        assert [pick.item.name for pick in selected] == ["Low best", "Mid", "High"]

    def test_empty_bands_are_skipped(self) -> None:
        picks = [_gather_pick(min_level=50)]
        selected = band_picks(picks)
        assert [band_for_level(pick.min_level) for pick in selected] == ["45-60"]

    def test_no_picks_returns_empty(self) -> None:
        assert band_picks([]) == []


class TestFormatChange:
    def test_positive_change_reads_up(self) -> None:
        assert format_change(12.0) == "up 12%"

    def test_negative_change_reads_down(self) -> None:
        assert format_change(-8.0) == "down 8%"

    def test_none_without_history(self) -> None:
        assert format_change(None) is None

    def test_rounds_to_nearest_percent(self) -> None:
        assert format_change(11.6) == "up 12%"

    def test_under_half_a_percent_reads_steady_not_up_0(self) -> None:
        assert format_change(0.4) == "steady"
        assert format_change(-0.4) == "steady"


class TestFormatMoney:
    @pytest.mark.parametrize(
        ("copper", "text"),
        [
            (33, "33c"),
            (190, "1s 90c"),
            (2203, "22s"),
            (3488, "35s"),
            (42382, "4g 24s"),
            (99999, "10g"),
            (1279557, "128g"),
            (5599999, "560g"),
            (12345678, "1,235g"),
        ],
    )
    def test_rounds_to_what_matters_at_each_size(self, copper: int, text: str) -> None:
        assert format_money(copper) == text


class TestDataAge:
    def test_reads_naturally(self) -> None:
        at = datetime(2026, 11, 20, 14, 0, tzinfo=UTC)
        assert data_age(at, at + timedelta(minutes=40)) == "Data from the last hour."
        assert data_age(at, at + timedelta(minutes=70)) == "Data from 1 hour ago."
        assert data_age(at, at + timedelta(hours=6)) == "Data from 6 hours ago."


class TestGatherPickLines:
    def test_lines_match_the_contract_format(self) -> None:
        pick = _gather_pick()
        line1, line2 = gather_pick_lines(pick)

        assert line1 == f"`Lvl 10-30` **[Kingsblood]({BASE_URL}/items/detail/?id=1)** 48s each, up 12%"
        assert line2 == "Arathi Highlands, Stranglethorn Vale. Skill 125. 210 sold a day."

    def test_no_skill_clause_when_skill_is_zero(self) -> None:
        pick = _gather_pick(skill=0, zones=[_zone("Elwynn Forest", 1, 10)])
        _line1, line2 = gather_pick_lines(pick)
        assert line2 == "Elwynn Forest. 210 sold a day."

    def test_no_change_suffix_without_history(self) -> None:
        pick = _gather_pick(change_pct=None)
        line1, _line2 = gather_pick_lines(pick)
        assert line1.endswith("48s each")

    def test_large_sold_per_day_gets_a_thousands_separator(self) -> None:
        pick = _gather_pick(sold_per_day=47234.0)
        _line1, line2 = gather_pick_lines(pick)
        assert line2.endswith("47,234 sold a day.")


class TestCraftPickLines:
    def test_lines_match_the_contract_format(self) -> None:
        pick = _craft_pick()
        line1, line2 = craft_pick_lines(pick)

        assert line1 == f"**[Major Healing Potion]({BASE_URL}/items/detail/?id=2)** +4g 10s profit a craft"
        assert line2 == "Materials 2g 30s. Sells for 6g 80s. 35 sold a day."

    def test_negative_profit_shows_minus(self) -> None:
        pick = _craft_pick(profit=-500)
        line1, _line2 = craft_pick_lines(pick)
        assert "-5s profit a craft" in line1

    def test_large_sold_per_day_gets_a_thousands_separator(self) -> None:
        pick = _craft_pick(sold_per_day=12040.0)
        _line1, line2 = craft_pick_lines(pick)
        assert line2.endswith("12,040 sold a day.")

    def test_no_cooldown_clause_without_a_cooldown(self) -> None:
        pick = _craft_pick(cooldown_hours=None)
        _line1, line2 = craft_pick_lines(pick)
        assert not line2.endswith(".Once")
        assert "Once" not in line2

    def test_daily_cooldown_reads_once_a_day(self) -> None:
        pick = _craft_pick(cooldown_hours=24)
        _line1, line2 = craft_pick_lines(pick)
        assert line2.endswith("35 sold a day. Once a day.")

    def test_two_day_cooldown(self) -> None:
        pick = _craft_pick(cooldown_hours=48)
        _line1, line2 = craft_pick_lines(pick)
        assert line2.endswith("Once every 2 days.")

    def test_four_day_cooldown(self) -> None:
        pick = _craft_pick(cooldown_hours=96)
        _line1, line2 = craft_pick_lines(pick)
        assert line2.endswith("Once every 4 days.")

    def test_other_cooldown_reads_once_every_n_hours(self) -> None:
        pick = _craft_pick(cooldown_hours=6)
        _line1, line2 = craft_pick_lines(pick)
        assert line2.endswith("Once every 6 hours.")


class TestFormatSoldPerDay:
    def test_rounds_and_adds_thousands_separator(self) -> None:
        assert format_sold_per_day(12039.6) == "12,040"

    def test_small_numbers_have_no_separator(self) -> None:
        assert format_sold_per_day(210.0) == "210"


class TestFormatCooldown:
    def test_none_without_a_cooldown(self) -> None:
        assert format_cooldown(None) is None

    def test_known_bands(self) -> None:
        assert format_cooldown(24) == "Once a day."
        assert format_cooldown(48) == "Once every 2 days."
        assert format_cooldown(96) == "Once every 4 days."

    def test_other_hours(self) -> None:
        assert format_cooldown(6) == "Once every 6 hours."
        assert format_cooldown(12) == "Once every 12 hours."


class TestBuildReportEmbeds:
    def test_gather_embed_structure(self) -> None:
        report = _report()
        now = datetime(2026, 11, 20, 20, 0, tzinfo=UTC)  # 6 hours after data_at

        gather_embed, craft_embed = build_report_embeds(report, BASE_URL, now=now)

        assert gather_embed.title == "Market report for Friday, November 20"
        assert gather_embed.url == f"{BASE_URL}/market/"
        assert gather_embed.description == (
            "What sells on our auction house today. Prices are what items sold for in the last two weeks."
        )
        assert gather_embed.colour == discord.Colour(0xD8AB5C)
        assert [field.name for field in gather_embed.fields] == ["Herbalism"]

        assert craft_embed.title == "Crafting"
        assert [field.name for field in craft_embed.fields] == ["Alchemy"]
        assert craft_embed.footer.text == "Stormwind. Data from 6 hours ago."
        assert craft_embed.timestamp == report.data_at

    def test_title_is_the_posting_day_even_when_the_data_is_from_last_night(self) -> None:
        report = _report()
        now = report.data_at + timedelta(hours=20)  # the next morning, Central
        gather_embed, _craft_embed = build_report_embeds(report, BASE_URL, now=now)
        assert gather_embed.title == f"Market report for {now.astimezone(GUILD_TZ):%A, %B %-d}"
        assert gather_embed.title != f"Market report for {report.data_at.astimezone(GUILD_TZ):%A, %B %-d}"

    def test_sections_appear_in_contract_order_and_skip_empty(self) -> None:
        report = _report(
            gathering=[
                MarketGatherSection(profession="Fishing", picks=[_gather_pick()]),
                MarketGatherSection(profession="Herbalism", picks=[_gather_pick()]),
                MarketGatherSection(profession="Mining", picks=[]),  # empty: skipped
            ]
        )

        gather_embed, _craft_embed = build_report_embeds(report, BASE_URL)

        names = [field.name for field in gather_embed.fields]
        assert names == [p for p in GATHER_PROFESSIONS if p in ("Herbalism", "Fishing")]

    def test_field_value_never_exceeds_discord_limit(self) -> None:
        long_name = "X" * 200
        picks = [
            _gather_pick(item=_item(item_id=i, name=f"{long_name}{i}"), min_level=level)
            for i, level in enumerate((1, 2, 3, 25, 26, 45, 46))
        ]
        report = _report(gathering=[MarketGatherSection(profession="Herbalism", picks=picks)])

        gather_embed, _craft_embed = build_report_embeds(report, BASE_URL)

        for field in gather_embed.fields:
            assert len(field.value) <= 1024

    def test_combined_embed_length_never_exceeds_discord_total_limit(self) -> None:
        long_name = "Y" * 200
        gathering = [
            MarketGatherSection(
                profession=profession,
                picks=[
                    _gather_pick(item=_item(item_id=100 + i, name=f"{long_name}{i}"), min_level=level)
                    for i, level in enumerate((1, 30, 50))
                ],
            )
            for profession in GATHER_PROFESSIONS
        ]
        crafting = [
            MarketCraftSection(
                profession=f"Profession{i}",
                picks=[
                    _craft_pick(item=_item(item_id=200 + i * 2, name=f"{long_name}{i}a")),
                    _craft_pick(item=_item(item_id=200 + i * 2 + 1, name=f"{long_name}{i}b")),
                ],
            )
            for i in range(10)
        ]
        report = _report(gathering=gathering, crafting=crafting)

        gather_embed, craft_embed = build_report_embeds(report, BASE_URL)

        assert len(gather_embed) + len(craft_embed) <= 6000


class TestBuildReportView:
    def test_has_full_report_and_this_week_link_buttons(self) -> None:
        view = build_report_view(BASE_URL)
        buttons = [item for item in view.children if isinstance(item, discord.ui.Button)]
        assert [(button.label, button.url) for button in buttons] == [
            ("Full report", f"{BASE_URL}/market/"),
            ("This week", f"{BASE_URL}/market/?view=week"),
        ]


def _make_cog(
    *,
    api: object,
    guilds: list[object] | None = None,
    channel_id: int | None = 777,
    last_day: str | None = None,
) -> MarketReportCog:
    cog = MarketReportCog.__new__(MarketReportCog)
    configs = AsyncMock()
    configs.get.return_value = SimpleNamespace(market_report_channel_id=channel_id, market_report_last_day=last_day)
    cog.bot = SimpleNamespace(api=api, guilds=guilds or [], configs=configs)
    return cog


def _fake_guild(*, guild_id: int = 42, channel: object | None = None) -> MagicMock:
    guild = MagicMock(spec=discord.Guild)
    guild.id = guild_id
    guild.get_channel.return_value = channel
    return guild


def _fake_channel() -> MagicMock:
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 777
    channel.send = AsyncMock(return_value=SimpleNamespace(id=1000, channel=channel))
    return channel


async def _check_at_1000_central(cog: MarketReportCog, guild: MagicMock) -> None:
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(discord.utils, "utcnow", lambda: datetime(2026, 11, 20, 16, 0, tzinfo=UTC))
        await cog._check_guild(guild)


class TestCheckGuild:
    async def test_posts_the_report_and_marks_the_day(self) -> None:
        channel = _fake_channel()
        guild = _fake_guild(channel=channel)
        api = AsyncMock()
        api.get_market_report.return_value = _report()
        cog = _make_cog(api=api, last_day=None)

        await _check_at_1000_central(cog, guild)

        channel.send.assert_awaited_once()
        update_arg = cog.bot.configs.update.await_args.args[1]
        assert update_arg.market_report_last_day == "2026-11-20"

    async def test_no_channel_configured_does_nothing(self) -> None:
        guild = _fake_guild()
        api = AsyncMock()
        cog = _make_cog(api=api, channel_id=None)

        await _check_at_1000_central(cog, guild)

        api.get_market_report.assert_not_called()

    async def test_before_post_time_does_nothing(self) -> None:
        channel = _fake_channel()
        guild = _fake_guild(channel=channel)
        api = AsyncMock()
        cog = _make_cog(api=api, last_day=None)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(discord.utils, "utcnow", lambda: datetime(2026, 11, 20, 15, 0, tzinfo=UTC))  # 09:00 Central
            await cog._check_guild(guild)

        channel.send.assert_not_awaited()
        api.get_market_report.assert_not_called()

    async def test_already_posted_today_does_nothing(self) -> None:
        channel = _fake_channel()
        guild = _fake_guild(channel=channel)
        api = AsyncMock()
        cog = _make_cog(api=api, last_day="2026-11-20")

        await _check_at_1000_central(cog, guild)

        channel.send.assert_not_awaited()
        api.get_market_report.assert_not_called()

    async def test_no_report_yet_skips_and_does_not_mark_the_day(self) -> None:
        channel = _fake_channel()
        guild = _fake_guild(channel=channel)
        api = AsyncMock()
        api.get_market_report.return_value = None
        cog = _make_cog(api=api, last_day=None)

        await _check_at_1000_central(cog, guild)

        channel.send.assert_not_awaited()
        cog.bot.configs.update.assert_not_awaited()

    async def test_stale_report_skips_and_does_not_mark_the_day(self) -> None:
        channel = _fake_channel()
        guild = _fake_guild(channel=channel)
        api = AsyncMock()
        api.get_market_report.return_value = _report(stale=True)
        cog = _make_cog(api=api, last_day=None)

        await _check_at_1000_central(cog, guild)

        channel.send.assert_not_awaited()
        cog.bot.configs.update.assert_not_awaited()


class TestCheckLoop:
    async def test_not_found_does_not_permanently_exclude_the_guild(self) -> None:
        guild = _fake_guild(channel=_fake_channel())
        api = AsyncMock()
        api.get_market_report.side_effect = [NotFoundError(404, "not found"), _report()]
        cog = _make_cog(api=api, guilds=[guild], last_day=None)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(discord.utils, "utcnow", lambda: datetime(2026, 11, 20, 16, 0, tzinfo=UTC))
            await cog._check_loop.coro(cog)
            await cog._check_loop.coro(cog)

        assert api.get_market_report.await_count == 2
        cog.bot.configs.update.assert_awaited_once()

    async def test_api_error_does_not_crash_the_loop(self) -> None:
        guild = _fake_guild(channel=_fake_channel())
        api = AsyncMock()
        api.get_market_report.side_effect = APIError(500, "boom")
        cog = _make_cog(api=api, guilds=[guild], last_day=None)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(discord.utils, "utcnow", lambda: datetime(2026, 11, 20, 16, 0, tzinfo=UTC))
            await cog._check_loop.coro(cog)  # must not raise
