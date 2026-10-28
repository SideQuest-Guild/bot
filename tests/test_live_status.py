"""Unit tests for the live-status plugin: build_voice_counts, the report loop, and the debounced voice listener."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from sidequest_bot.api_client import APIError, NotFoundError
from sidequest_bot.plugins import live_status
from sidequest_bot.plugins.live_status import LiveStatusCog, build_voice_counts
from sidequest_common.schemas import DiscordLiveReport, VoiceChannelCount


def _fake_category(*, category_id: int, position: int) -> MagicMock:
    category = MagicMock(spec=discord.CategoryChannel)
    category.id = category_id
    category.position = position
    return category


def _fake_voice_channel(
    *,
    name: str,
    position: int,
    category: MagicMock | None = None,
    hidden: bool = False,
    members: list[MagicMock] | None = None,
    visible_to: set[object] | None = None,
) -> MagicMock:
    channel = MagicMock(spec=discord.VoiceChannel)
    channel.name = name
    channel.position = position
    channel.category = category
    channel.members = members or []
    if visible_to is not None:
        channel.permissions_for.side_effect = lambda role: SimpleNamespace(view_channel=role in visible_to)
    else:
        channel.permissions_for.return_value = SimpleNamespace(view_channel=not hidden)
    return channel


def _fake_member(*, bot: bool = False) -> MagicMock:
    member = MagicMock(spec=discord.Member)
    member.bot = bot
    return member


def _fake_guild(
    *, guild_id: int = 42, voice_channels: list[MagicMock] | None = None, stage_channels: list[MagicMock] | None = None
) -> MagicMock:
    guild = MagicMock(spec=discord.Guild)
    guild.id = guild_id
    guild.default_role = MagicMock(spec=discord.Role)
    guild.voice_channels = voice_channels or []
    guild.stage_channels = stage_channels or []
    guild.get_role = MagicMock(return_value=None)
    return guild


def _configs(*, linked_role_id: int | None = None) -> AsyncMock:
    configs = AsyncMock()
    configs.get.return_value = SimpleNamespace(linked_role_id=linked_role_id)
    return configs


def _make_cog(*, api: object, guilds: list[object] | None = None, bot: object | None = None) -> LiveStatusCog:
    cog = LiveStatusCog.__new__(LiveStatusCog)
    cog.bot = bot or SimpleNamespace(api=api, configs=_configs(), guilds=guilds or [], fetch_guild=AsyncMock())
    cog._not_site_guild_ids = set()
    cog._last_online = {}
    cog._last_members = {}
    cog._debounce_tasks = {}
    return cog


class TestBuildVoiceCounts:
    def test_excludes_hidden_channels(self) -> None:
        visible = _fake_voice_channel(name="Lobby", position=0)
        hidden = _fake_voice_channel(name="Secret", position=1, hidden=True)
        guild = _fake_guild(voice_channels=[visible, hidden])

        result = build_voice_counts(guild, guild.default_role)

        assert [item.name for item in result] == ["Lobby"]

    def test_excludes_bots_from_count(self) -> None:
        channel = _fake_voice_channel(
            name="Lobby", position=0, members=[_fake_member(), _fake_member(bot=True), _fake_member()]
        )
        guild = _fake_guild(voice_channels=[channel])

        result = build_voice_counts(guild, guild.default_role)

        assert result == [VoiceChannelCount(name="Lobby", count=2)]

    def test_orders_by_category_then_channel_position(self) -> None:
        cat_a = _fake_category(category_id=1, position=0)
        cat_b = _fake_category(category_id=2, position=1)
        second_in_a = _fake_voice_channel(name="second-a", position=1, category=cat_a)
        first_in_a = _fake_voice_channel(name="first-a", position=0, category=cat_a)
        first_in_b = _fake_voice_channel(name="first-b", position=0, category=cat_b)
        uncategorized = _fake_voice_channel(name="no-cat", position=0, category=None)
        guild = _fake_guild(voice_channels=[second_in_a, first_in_a, first_in_b, uncategorized])

        result = build_voice_counts(guild, guild.default_role)

        assert [item.name for item in result] == ["no-cat", "first-a", "second-a", "first-b"]

    def test_zero_count_channels_included(self) -> None:
        channel = _fake_voice_channel(name="Empty", position=0, members=[])
        guild = _fake_guild(voice_channels=[channel])

        result = build_voice_counts(guild, guild.default_role)

        assert result == [VoiceChannelCount(name="Empty", count=0)]

    def test_includes_stage_channels(self) -> None:
        stage = MagicMock(spec=discord.StageChannel)
        stage.name = "Stage"
        stage.position = 0
        stage.category = None
        stage.members = []
        stage.permissions_for.return_value = SimpleNamespace(view_channel=True)
        guild = _fake_guild(stage_channels=[stage])

        result = build_voice_counts(guild, guild.default_role)

        assert [item.name for item in result] == ["Stage"]

    def test_counts_channel_hidden_from_everyone_but_visible_to_audience(self) -> None:
        linked_role = MagicMock(spec=discord.Role)
        everyone = MagicMock(spec=discord.Role)
        site_only = _fake_voice_channel(name="Site Lounge", position=0, visible_to={linked_role})
        guild = _fake_guild(voice_channels=[site_only])
        guild.default_role = everyone

        assert [item.name for item in build_voice_counts(guild, linked_role)] == ["Site Lounge"]
        assert build_voice_counts(guild, everyone) == []

    def test_excludes_channel_hidden_from_the_audience_role(self) -> None:
        linked_role = MagicMock(spec=discord.Role)
        officer_only = _fake_voice_channel(name="Officer Hall", position=0, visible_to=set())
        guild = _fake_guild(voice_channels=[officer_only])

        result = build_voice_counts(guild, linked_role)

        assert result == []


class TestReportLoop:
    async def test_refreshes_counts_then_pushes(self) -> None:
        guild = _fake_guild()
        api = AsyncMock()
        fetched = SimpleNamespace(approximate_presence_count=5, approximate_member_count=20)
        bot = SimpleNamespace(api=api, configs=_configs(), guilds=[guild], fetch_guild=AsyncMock(return_value=fetched))
        cog = _make_cog(api=api, bot=bot)

        await cog._report_loop.coro(cog)

        api.report_discord_live.assert_awaited_once_with(guild.id, DiscordLiveReport(online=5, members=20, voice=[]))

    async def test_keeps_last_known_counts_on_fetch_failure(self) -> None:
        guild = _fake_guild()
        api = AsyncMock()
        bot = SimpleNamespace(
            api=api,
            configs=_configs(),
            guilds=[guild],
            fetch_guild=AsyncMock(side_effect=discord.HTTPException(MagicMock(), "boom")),
        )
        cog = _make_cog(api=api, bot=bot)
        cog._last_online[guild.id] = 3
        cog._last_members[guild.id] = 10

        await cog._report_loop.coro(cog)

        api.report_discord_live.assert_awaited_once_with(guild.id, DiscordLiveReport(online=3, members=10, voice=[]))

    async def test_not_found_stops_reporting_that_guild(self) -> None:
        guild = _fake_guild()
        api = AsyncMock()
        api.report_discord_live.side_effect = NotFoundError(404, "not the site guild")
        fetched = SimpleNamespace(approximate_presence_count=1, approximate_member_count=2)
        bot = SimpleNamespace(api=api, configs=_configs(), guilds=[guild], fetch_guild=AsyncMock(return_value=fetched))
        cog = _make_cog(api=api, bot=bot)

        await cog._report_loop.coro(cog)
        await cog._report_loop.coro(cog)

        assert guild.id in cog._not_site_guild_ids
        assert api.report_discord_live.await_count == 1

    async def test_other_api_errors_do_not_crash_the_loop(self) -> None:
        guild = _fake_guild()
        api = AsyncMock()
        api.report_discord_live.side_effect = APIError(500, "boom")
        fetched = SimpleNamespace(approximate_presence_count=1, approximate_member_count=2)
        bot = SimpleNamespace(api=api, configs=_configs(), guilds=[guild], fetch_guild=AsyncMock(return_value=fetched))
        cog = _make_cog(api=api, bot=bot)

        await cog._report_loop.coro(cog)  # must not raise

        assert guild.id not in cog._not_site_guild_ids


class TestPushAudience:
    async def test_uses_linked_role_as_audience_when_present(self) -> None:
        linked_role = MagicMock(spec=discord.Role)
        guild = _fake_guild()
        guild.get_role.return_value = linked_role
        api = AsyncMock()
        bot = SimpleNamespace(api=api, configs=_configs(linked_role_id=100), guilds=[guild], fetch_guild=AsyncMock())
        cog = _make_cog(api=api, bot=bot)

        with patch.object(live_status, "build_voice_counts", return_value=[]) as mocked:
            await cog._push(guild)

        guild.get_role.assert_called_once_with(100)
        mocked.assert_called_once_with(guild, linked_role)

    async def test_falls_back_to_everyone_when_no_linked_role_configured(self) -> None:
        guild = _fake_guild()
        api = AsyncMock()
        bot = SimpleNamespace(api=api, configs=_configs(linked_role_id=None), guilds=[guild], fetch_guild=AsyncMock())
        cog = _make_cog(api=api, bot=bot)

        with patch.object(live_status, "build_voice_counts", return_value=[]) as mocked:
            await cog._push(guild)

        mocked.assert_called_once_with(guild, guild.default_role)

    async def test_falls_back_when_linked_role_no_longer_in_guild(self) -> None:
        guild = _fake_guild()
        guild.get_role.return_value = None
        api = AsyncMock()
        bot = SimpleNamespace(api=api, configs=_configs(linked_role_id=999), guilds=[guild], fetch_guild=AsyncMock())
        cog = _make_cog(api=api, bot=bot)

        with patch.object(live_status, "build_voice_counts", return_value=[]) as mocked:
            await cog._push(guild)

        mocked.assert_called_once_with(guild, guild.default_role)

    async def test_config_fetch_failure_falls_back_without_crashing(self) -> None:
        guild = _fake_guild()
        api = AsyncMock()
        configs = AsyncMock()
        configs.get.side_effect = APIError(500, "boom")
        bot = SimpleNamespace(api=api, configs=configs, guilds=[guild], fetch_guild=AsyncMock())
        cog = _make_cog(api=api, bot=bot)

        with patch.object(live_status, "build_voice_counts", return_value=[]) as mocked:
            await cog._push(guild)  # must not raise

        mocked.assert_called_once_with(guild, guild.default_role)
        api.report_discord_live.assert_awaited_once()


class TestVoiceStateListener:
    @pytest.fixture(autouse=True)
    def _fast_debounce(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(live_status, "DEBOUNCE_SECONDS", 0.01)

    def _state(self, channel: object | None) -> MagicMock:
        state = MagicMock(spec=discord.VoiceState)
        state.channel = channel
        return state

    async def test_channel_change_pushes_after_debounce(self) -> None:
        guild = _fake_guild()
        member = MagicMock(spec=discord.Member)
        member.guild = guild
        api = AsyncMock()
        cog = _make_cog(api=api)

        await cog.on_voice_state_update(member, self._state(None), self._state(object()))
        await asyncio.sleep(0.05)

        api.report_discord_live.assert_awaited_once()

    async def test_mute_deafen_toggle_does_not_push(self) -> None:
        guild = _fake_guild()
        member = MagicMock(spec=discord.Member)
        member.guild = guild
        channel = object()
        api = AsyncMock()
        cog = _make_cog(api=api)

        await cog.on_voice_state_update(member, self._state(channel), self._state(channel))
        await asyncio.sleep(0.05)

        api.report_discord_live.assert_not_awaited()

    async def test_burst_of_changes_coalesces_into_one_push(self) -> None:
        guild = _fake_guild()
        member = MagicMock(spec=discord.Member)
        member.guild = guild
        api = AsyncMock()
        cog = _make_cog(api=api)

        for _ in range(5):
            await cog.on_voice_state_update(member, self._state(None), self._state(object()))
        await asyncio.sleep(0.05)

        api.report_discord_live.assert_awaited_once()

    async def test_skips_known_non_site_guild(self) -> None:
        guild = _fake_guild()
        member = MagicMock(spec=discord.Member)
        member.guild = guild
        api = AsyncMock()
        cog = _make_cog(api=api)
        cog._not_site_guild_ids.add(guild.id)

        await cog.on_voice_state_update(member, self._state(None), self._state(object()))
        await asyncio.sleep(0.05)

        api.report_discord_live.assert_not_awaited()
