"""Unit tests for the roster sync plugin: the summary message, milestone feed, and the periodic loop's errors."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import discord

from sidequest_bot.api_client import APIError, CharacterMilestone, NotFoundError, RosterSyncReport
from sidequest_bot.plugins.roster_sync import RosterSyncCog, build_milestone_embed, describe_sync


def _report(**overrides: object) -> RosterSyncReport:
    base: dict[str, object] = {
        "members": 8,
        "joined": [],
        "left": [],
        "profiles_refreshed": 0,
        "characters_linked": 0,
    }
    base.update(overrides)
    return RosterSyncReport.model_validate(base)


def _milestone(**overrides: object) -> CharacterMilestone:
    base: dict[str, object] = {
        "id": uuid4(),
        "slug": "nox-zul",
        "name": "Nox",
        "surname": "Zul",
        "class": "mage",
        "level": 60,
        "reached_at": datetime(2026, 1, 1, tzinfo=UTC),
        "posted_at": None,
    }
    base.update(overrides)
    return CharacterMilestone.model_validate(base)


def _fake_guild(guild_id: int = 42, *, channel: object | None = None) -> MagicMock:
    guild = MagicMock(spec=discord.Guild)
    guild.id = guild_id
    guild.get_channel.return_value = channel
    return guild


def _fake_channel(*, message_id: int = 999) -> MagicMock:
    channel = MagicMock(spec=discord.TextChannel)
    channel.send = AsyncMock(return_value=SimpleNamespace(id=message_id))
    return channel


def _make_cog(
    *,
    api: object | None = None,
    guilds: list[object] | None = None,
    channel_id: int | None = None,
    pending_milestones: list[object] | None = None,
) -> RosterSyncCog:
    cog = RosterSyncCog.__new__(RosterSyncCog)
    api = api or AsyncMock()
    api.list_pending_milestones.return_value = pending_milestones if pending_milestones is not None else []
    configs = AsyncMock()
    configs.get.return_value = SimpleNamespace(announcement_channel_id=channel_id)
    cog.bot = SimpleNamespace(api=api, guilds=guilds or [], configs=configs)
    return cog


class TestDescribeSync:
    def test_unchanged_roster_reports_nothing(self) -> None:
        assert describe_sync(_report()) is None

    def test_summarizes_joins_leaves_refreshes_and_links(self) -> None:
        report = _report(joined=["Nox Zul"], left=["Old Member"], profiles_refreshed=3, characters_linked=1)
        summary = describe_sync(report)
        assert summary is not None
        assert "joined: Nox Zul" in summary
        assert "left: Old Member" in summary
        assert "3 profile(s) refreshed" in summary
        assert "1 character(s) linked" in summary


class TestSyncLoop:
    async def test_not_found_retries_next_tick_and_recovers(self) -> None:
        """A 404 from a not-yet-deployed sync route (rolling deploy) must not permanently disable the cog."""
        guild = _fake_guild()
        api = AsyncMock()
        api.sync_roster.side_effect = [NotFoundError(404, "not deployed yet"), _report()]
        cog = _make_cog(api=api, guilds=[guild])

        await cog._sync_loop.coro(cog)
        await cog._sync_loop.coro(cog)

        assert api.sync_roster.await_count == 2

    async def test_service_unavailable_does_not_crash_the_loop(self) -> None:
        guild = _fake_guild()
        api = AsyncMock()
        api.sync_roster.side_effect = APIError(503, "Battle.net unconfigured")
        cog = _make_cog(api=api, guilds=[guild])

        await cog._sync_loop.coro(cog)  # must not raise

    async def test_other_api_error_does_not_crash_the_loop(self) -> None:
        guild = _fake_guild()
        api = AsyncMock()
        api.sync_roster.side_effect = APIError(500, "boom")
        cog = _make_cog(api=api, guilds=[guild])

        await cog._sync_loop.coro(cog)  # must not raise

    async def test_syncs_every_guild_each_tick(self) -> None:
        guilds = [_fake_guild(1), _fake_guild(2)]
        api = AsyncMock()
        api.sync_roster.return_value = _report()
        cog = _make_cog(api=api, guilds=guilds)

        await cog._sync_loop.coro(cog)

        assert api.sync_roster.await_count == 2
        api.sync_roster.assert_any_await(1)
        api.sync_roster.assert_any_await(2)


class TestBuildMilestoneEmbed:
    def test_title_color_url_and_timestamp(self) -> None:
        milestone = _milestone()
        embed = build_milestone_embed(milestone)
        assert embed.title == "Nox Zul reached level 60!"
        assert embed.color == discord.Colour(0x69CCF0)
        assert embed.url == "https://sidequest-guild.com/characters/detail/?slug=nox-zul"
        assert embed.timestamp == milestone.reached_at


class TestPostMilestones:
    async def test_no_channel_configured_leaves_them_pending(self) -> None:
        guild = _fake_guild()
        api = AsyncMock()
        cog = _make_cog(api=api, channel_id=None, pending_milestones=[_milestone()])

        await cog._post_milestones(guild)

        api.mark_milestone_posted.assert_not_awaited()

    async def test_channel_not_resolvable_leaves_them_pending(self) -> None:
        guild = _fake_guild(channel=None)
        api = AsyncMock()
        cog = _make_cog(api=api, channel_id=777, pending_milestones=[_milestone()])

        await cog._post_milestones(guild)

        api.mark_milestone_posted.assert_not_awaited()

    async def test_not_found_fetching_pending_does_not_raise(self) -> None:
        """A 404 from a not-yet-deployed milestones route must not stop the sync loop."""
        guild = _fake_guild()
        api = AsyncMock()
        api.list_pending_milestones.side_effect = NotFoundError(404, "not deployed yet")
        cog = _make_cog(api=api, channel_id=777)

        await cog._post_milestones(guild)  # must not raise

    async def test_no_pending_milestones_does_not_touch_the_channel(self) -> None:
        guild = _fake_guild()
        api = AsyncMock()
        cog = _make_cog(api=api, channel_id=777, pending_milestones=[])

        await cog._post_milestones(guild)

        cog.bot.configs.get.assert_not_awaited()

    async def test_posts_each_pending_milestone_and_marks_posted(self) -> None:
        channel = _fake_channel(message_id=1234)
        guild = _fake_guild(channel=channel)
        api = AsyncMock()
        first, second = _milestone(level=40), _milestone(level=60)
        cog = _make_cog(api=api, channel_id=777, pending_milestones=[first, second])

        await cog._post_milestones(guild)

        assert channel.send.await_count == 2
        assert api.mark_milestone_posted.await_count == 2
        api.mark_milestone_posted.assert_any_await(guild.id, first.id, 1234)
        api.mark_milestone_posted.assert_any_await(guild.id, second.id, 1234)

    async def test_one_posting_failure_does_not_stop_the_others(self) -> None:
        channel = _fake_channel()
        channel.send.side_effect = [discord.HTTPException(MagicMock(), "boom"), SimpleNamespace(id=42)]
        guild = _fake_guild(channel=channel)
        api = AsyncMock()
        first, second = _milestone(level=40), _milestone(level=60)
        cog = _make_cog(api=api, channel_id=777, pending_milestones=[first, second])

        await cog._post_milestones(guild)  # must not raise

        assert channel.send.await_count == 2
        api.mark_milestone_posted.assert_awaited_once_with(guild.id, second.id, 42)


class TestPostMilestone:
    async def test_discord_send_failure_does_not_raise_and_never_acks(self) -> None:
        channel = _fake_channel()
        channel.send.side_effect = discord.HTTPException(MagicMock(), "boom")
        guild = _fake_guild(channel=channel)
        api = AsyncMock()
        cog = _make_cog(api=api)

        await cog._post_milestone(guild, channel, _milestone())  # must not raise

        api.mark_milestone_posted.assert_not_awaited()

    async def test_ack_failure_does_not_raise(self) -> None:
        channel = _fake_channel()
        guild = _fake_guild(channel=channel)
        api = AsyncMock()
        api.mark_milestone_posted.side_effect = APIError(500, "boom")
        cog = _make_cog(api=api)

        await cog._post_milestone(guild, channel, _milestone())  # must not raise

        channel.send.assert_awaited_once()
