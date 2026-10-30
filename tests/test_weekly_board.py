"""Unit tests for the weekly-board plugin: week window math, idempotency, and embed content."""

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4
from zoneinfo import ZoneInfo

import discord
import pytest

from sidequest_bot.api_client import APIError, NotFoundError
from sidequest_bot.plugins.weekly_board import (
    GUILD_TZ,
    WeeklyBoardCog,
    build_board_embed,
    should_post,
    week_monday,
    week_window,
)
from sidequest_common.schemas import EventKind, WeeklyBoardEvent


def _event(**overrides: object) -> WeeklyBoardEvent:
    base: dict[str, object] = {
        "id": uuid4(),
        "kind": EventKind.RAID,
        "title": "Molten Core",
        "starts_at": datetime(2026, 11, 3, 19, 0, tzinfo=UTC),
        "discord_thread_id": None,
    }
    base.update(overrides)
    return WeeklyBoardEvent.model_validate(base)


class TestWeekMonday:
    def test_mid_week_returns_the_mondays_date(self) -> None:
        # Wednesday Nov 4, 2026 in Chicago.
        now = datetime(2026, 11, 4, 15, 0, tzinfo=ZoneInfo("America/Chicago"))
        assert week_monday(now) == date(2026, 11, 2)

    def test_sunday_still_counts_as_the_prior_monday(self) -> None:
        now = datetime(2026, 11, 8, 23, 59, tzinfo=ZoneInfo("America/Chicago"))
        assert week_monday(now) == date(2026, 11, 2)

    def test_converts_from_another_timezone_before_taking_the_date(self) -> None:
        # 2026-11-02 05:00 UTC is still Sunday Nov 1 in Chicago (UTC-6 after DST ends).
        now = datetime(2026, 11, 2, 5, 0, tzinfo=UTC)
        assert week_monday(now) == date(2026, 10, 26)


class TestWeekWindow:
    def test_spans_exactly_one_week_in_chicago_time(self) -> None:
        start, end = week_window(date(2026, 11, 2))
        assert start == datetime(2026, 11, 2, 6, 0, tzinfo=UTC)  # CST is UTC-6 after Nov 1 DST end
        assert end == datetime(2026, 11, 9, 6, 0, tzinfo=UTC)
        assert end - start == timedelta(days=7)

    def test_dst_spring_forward_mid_week_shortens_the_utc_span(self) -> None:
        # DST begins 2026-03-08 (US); the week of Mar 2 loses an hour in Chicago wall-clock terms but each bound
        # still lands on exactly 00:00 local time.
        start, end = week_window(date(2026, 3, 2))
        assert start.astimezone(GUILD_TZ) == datetime(2026, 3, 2, 0, 0, tzinfo=GUILD_TZ)
        assert end.astimezone(GUILD_TZ) == datetime(2026, 3, 9, 0, 0, tzinfo=GUILD_TZ)
        assert (end - start).total_seconds() == 6 * 24 * 3600 + 23 * 3600  # one hour short of 7 full days in UTC


class TestShouldPost:
    def test_before_post_time_on_monday_does_not_post(self) -> None:
        now = datetime(2026, 11, 2, 9, 59, tzinfo=GUILD_TZ)
        assert should_post(now, last_week=None) is None

    def test_at_or_after_post_time_on_monday_posts(self) -> None:
        now = datetime(2026, 11, 2, 10, 0, tzinfo=GUILD_TZ)
        assert should_post(now, last_week=None) == date(2026, 11, 2)

    def test_already_posted_this_week_does_not_post_again(self) -> None:
        now = datetime(2026, 11, 2, 14, 0, tzinfo=GUILD_TZ)
        assert should_post(now, last_week="2026-11-02") is None

    def test_restart_later_the_same_monday_still_catches_up(self) -> None:
        now = datetime(2026, 11, 2, 18, 0, tzinfo=GUILD_TZ)
        assert should_post(now, last_week="2026-10-26") == date(2026, 11, 2)

    def test_non_monday_never_posts_even_if_unposted(self) -> None:
        now = datetime(2026, 11, 3, 12, 0, tzinfo=GUILD_TZ)
        assert should_post(now, last_week="2026-10-26") is None

    def test_missed_window_does_not_post_later_in_the_week(self) -> None:
        # The bot was down all of Monday Nov 2 and came back Wednesday: it must not post a late Monday board.
        now = datetime(2026, 11, 4, 9, 0, tzinfo=GUILD_TZ)
        assert should_post(now, last_week="2026-10-26") is None


class TestBuildBoardEmbed:
    def test_empty_week_posts_nothing(self) -> None:
        assert build_board_embed([], date(2026, 11, 2), "https://sidequest-guild.com", 42) is None

    def test_embed_lists_events_in_order_with_links_and_timestamps(self) -> None:
        events = [
            _event(title="Molten Core", starts_at=datetime(2026, 11, 3, 19, 0, tzinfo=UTC)),
            _event(
                title="Heroic Dungeons",
                kind=EventKind.DUNGEON,
                starts_at=datetime(2026, 11, 5, 19, 0, tzinfo=UTC),
                discord_thread_id=555,
            ),
        ]
        event_ids = [str(e.id) for e in events]
        embed = build_board_embed(events, date(2026, 11, 2), "https://sidequest-guild.com/", 42)
        assert embed is not None
        assert embed.title == "This Week's Events: Nov 2 - Nov 8"
        assert embed.colour == discord.Colour(0xF1C40F)
        description = embed.description or ""
        assert description.index(event_ids[0]) < description.index(event_ids[1])
        assert f"https://sidequest-guild.com/events/detail/?id={event_ids[0]}" in description
        assert "Raid: [Molten Core]" in description
        assert "Dungeon: [Heroic Dungeons]" in description
        assert "https://discord.com/channels/42/555" in description
        assert "[Thread]" in description
        timestamp = int(events[0].starts_at.timestamp())
        assert f"<t:{timestamp}:F>" in description
        assert f"<t:{timestamp}:R>" in description


def _make_cog(*, api: object, guilds: list[object] | None = None, last_week: str | None = None) -> WeeklyBoardCog:
    cog = WeeklyBoardCog.__new__(WeeklyBoardCog)
    configs = AsyncMock()
    configs.get.return_value = SimpleNamespace(weekly_board_channel_id=777, weekly_board_last_week=last_week)
    cog.bot = SimpleNamespace(api=api, guilds=guilds or [], configs=configs)
    return cog


def _fake_guild(*, guild_id: int = 42, channel: object | None = None) -> MagicMock:
    guild = MagicMock(spec=discord.Guild)
    guild.id = guild_id
    guild.get_channel.return_value = channel
    return guild


def _fake_channel() -> MagicMock:
    channel = MagicMock(spec=discord.TextChannel)
    channel.send = AsyncMock()
    return channel


class TestCheckGuild:
    async def test_posts_and_marks_the_week_handled(self) -> None:
        channel = _fake_channel()
        guild = _fake_guild(channel=channel)
        api = AsyncMock()
        api.list_weekly_board_events.return_value = [_event()]
        cog = _make_cog(api=api, last_week=None)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(discord.utils, "utcnow", lambda: datetime(2026, 11, 2, 16, 0, tzinfo=UTC))
            await cog._check_guild(guild)

        channel.send.assert_awaited_once()
        cog.bot.configs.update.assert_awaited_once()
        update_arg = cog.bot.configs.update.await_args.args[1]
        assert update_arg.weekly_board_last_week == "2026-11-02"

    async def test_does_not_double_post_the_same_week(self) -> None:
        channel = _fake_channel()
        guild = _fake_guild(channel=channel)
        api = AsyncMock()
        api.list_weekly_board_events.return_value = [_event()]
        cog = _make_cog(api=api, last_week="2026-11-02")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(discord.utils, "utcnow", lambda: datetime(2026, 11, 2, 16, 0, tzinfo=UTC))
            await cog._check_guild(guild)

        channel.send.assert_not_awaited()
        cog.bot.configs.update.assert_not_awaited()

    async def test_empty_week_sends_nothing_but_still_marks_the_week_handled(self) -> None:
        channel = _fake_channel()
        guild = _fake_guild(channel=channel)
        api = AsyncMock()
        api.list_weekly_board_events.return_value = []
        cog = _make_cog(api=api, last_week=None)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(discord.utils, "utcnow", lambda: datetime(2026, 11, 2, 16, 0, tzinfo=UTC))
            await cog._check_guild(guild)

        channel.send.assert_not_awaited()
        cog.bot.configs.update.assert_awaited_once()


class TestCheckLoop:
    async def test_not_found_does_not_permanently_exclude_the_guild(self) -> None:
        """A 404 from a new, not-yet-deployed endpoint must not disable the cog for the guild for good."""
        guild = _fake_guild(channel=_fake_channel())
        api = AsyncMock()
        api.list_weekly_board_events.side_effect = [NotFoundError(404, "not found"), []]
        cog = _make_cog(api=api, guilds=[guild], last_week=None)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(discord.utils, "utcnow", lambda: datetime(2026, 11, 2, 16, 0, tzinfo=UTC))
            await cog._check_loop.coro(cog)
            await cog._check_loop.coro(cog)

        assert api.list_weekly_board_events.await_count == 2
        cog.bot.configs.update.assert_awaited_once()

    async def test_api_error_does_not_crash_the_loop(self) -> None:
        guild = _fake_guild()
        api = AsyncMock()
        api.list_weekly_board_events.side_effect = APIError(500, "boom")
        cog = _make_cog(api=api, guilds=[guild], last_week=None)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(discord.utils, "utcnow", lambda: datetime(2026, 11, 2, 16, 0, tzinfo=UTC))
            await cog._check_loop.coro(cog)  # must not raise
