"""Unit tests for the roster sync plugin: the summary message and the periodic loop's error handling."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from sidequest_bot.api_client import APIError, NotFoundError, RosterSyncReport
from sidequest_bot.plugins.roster_sync import RosterSyncCog, describe_sync


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


def _fake_guild(guild_id: int = 42) -> MagicMock:
    guild = MagicMock(spec=discord.Guild)
    guild.id = guild_id
    return guild


def _make_cog(*, api: object | None = None, guilds: list[object] | None = None) -> RosterSyncCog:
    cog = RosterSyncCog.__new__(RosterSyncCog)
    cog.bot = SimpleNamespace(api=api or AsyncMock(), guilds=guilds or [])
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
