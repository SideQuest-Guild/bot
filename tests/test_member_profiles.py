"""Unit tests for the member-profiles plugin: pure builders, the sweep loop, and debounced listeners."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import discord
import pytest

from sidequest_bot.api_client import APIError, APIResponseShapeError, NotFoundError
from sidequest_bot.plugins import member_profiles as member_profiles_module
from sidequest_bot.plugins.member_profiles import (
    MemberProfilesCog,
    build_member_profile,
    build_sweep,
    plan_pending_push,
)
from sidequest_common.schemas import MemberLink, MemberProfileIn


@pytest.fixture(autouse=True)
def _fast_debounce(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(member_profiles_module, "DEBOUNCE_SECONDS", 0)


def _fake_role(role_id: int) -> MagicMock:
    role = MagicMock(spec=discord.Role)
    role.id = role_id
    return role


def _fake_asset(key: str) -> MagicMock:
    asset = MagicMock(spec=discord.Asset)
    asset.key = key
    return asset


def _fake_guild(*, guild_id: int = 42, members: list[MagicMock] | None = None) -> MagicMock:
    guild = MagicMock(spec=discord.Guild)
    guild.id = guild_id
    guild.default_role = _fake_role(guild_id)
    members_by_id = {member.id: member for member in (members or [])}
    guild.get_member.side_effect = members_by_id.get
    return guild


def _fake_member(
    member_id: int,
    *,
    guild: MagicMock,
    username: str = "old-username",
    global_name: str | None = None,
    nick: str | None = None,
    avatar: MagicMock | None = None,
    guild_avatar: MagicMock | None = None,
    roles: list[MagicMock] | None = None,
) -> MagicMock:
    member = MagicMock(spec=discord.Member)
    member.id = member_id
    member.guild = guild
    member.name = username
    member.global_name = global_name
    member.nick = nick
    member.avatar = avatar
    member.guild_avatar = guild_avatar
    member.roles = [guild.default_role, *(roles or [])]
    return member


def _make_cog(*, api: object | None = None, guilds: list[object] | None = None) -> MemberProfilesCog:
    cog = MemberProfilesCog.__new__(MemberProfilesCog)
    cog.bot = SimpleNamespace(api=api or AsyncMock(), guilds=guilds or [])
    cog._not_site_guild_ids = set()
    cog._linked_ids = {}
    cog._pending_ids = {}
    cog._debounce_tasks = {}
    return cog


class TestBuildMemberProfile:
    """Matches the fields ``MembersService.upsert_from_discord`` sets at sign-in."""

    def test_matches_sign_in_fields(self) -> None:
        guild = _fake_guild(guild_id=42)
        member = _fake_member(
            1,
            guild=guild,
            username="hailcaesar4288",
            global_name="HailCaesar",
            nick="Trey",
            avatar=_fake_asset("abc123"),
            roles=[_fake_role(100)],
        )

        profile = build_member_profile(member)

        assert profile == MemberProfileIn(
            discord_id=1,
            username="hailcaesar4288",
            global_name="HailCaesar",
            guild_nick="Trey",
            avatar_hash="abc123",
            roles=[100],
        )

    def test_no_avatar_is_null(self) -> None:
        member = _fake_member(1, guild=_fake_guild(), avatar=None)

        profile = build_member_profile(member)

        assert profile.avatar_hash is None

    def test_excludes_the_everyone_role(self) -> None:
        guild = _fake_guild(guild_id=42)
        member = _fake_member(1, guild=guild, roles=[_fake_role(100)])

        profile = build_member_profile(member)

        assert profile.roles == [100]


class TestBuildSweep:
    def test_profiles_for_present_linked_members_departed_for_absent(self) -> None:
        guild = _fake_guild(guild_id=42)
        present = _fake_member(1, guild=guild)
        guild.get_member.side_effect = {1: present}.get

        profiles, departed_ids = build_sweep(guild, {1, 2, 3})

        assert [profile.discord_id for profile in profiles] == [1]
        assert departed_ids == [2, 3]

    def test_empty_linked_ids_is_empty(self) -> None:
        profiles, departed_ids = build_sweep(_fake_guild(), set())

        assert profiles == []
        assert departed_ids == []


class TestPlanPendingPush:
    def test_skips_ids_outside_the_linked_set(self) -> None:
        guild = _fake_guild()
        member = _fake_member(1, guild=guild)
        guild.get_member.side_effect = {1: member}.get

        profiles, departed_ids = plan_pending_push(guild, {1, 99}, {1})

        assert [profile.discord_id for profile in profiles] == [1]
        assert departed_ids == []

    def test_departed_when_linked_but_no_longer_in_the_guild(self) -> None:
        guild = _fake_guild()
        guild.get_member.side_effect = {}.get

        profiles, departed_ids = plan_pending_push(guild, {5}, {5})

        assert profiles == []
        assert departed_ids == [5]


class TestSweepLoop:
    async def test_not_found_marks_guild_non_site(self) -> None:
        guild = _fake_guild()
        api = AsyncMock()
        api.list_member_links.side_effect = NotFoundError(404, "not the site guild")
        cog = _make_cog(api=api, guilds=[guild])

        await cog._sweep_loop.coro(cog)
        await cog._sweep_loop.coro(cog)

        assert guild.id in cog._not_site_guild_ids
        assert api.list_member_links.await_count == 1

    @pytest.mark.parametrize("error", [APIError(500, "boom"), APIResponseShapeError(502, "bad shape")])
    async def test_failed_list_links_does_not_crash_the_loop(self, error: APIError) -> None:
        guild = _fake_guild()
        api = AsyncMock()
        api.list_member_links.side_effect = error
        cog = _make_cog(api=api, guilds=[guild])

        await cog._sweep_loop.coro(cog)  # must not raise

        assert guild.id not in cog._not_site_guild_ids

    async def test_pushes_profiles_and_departed_from_the_linked_set(self) -> None:
        guild = _fake_guild(guild_id=42)
        present = _fake_member(1, guild=guild, username="hailcaesar4288")
        guild.get_member.side_effect = {1: present}.get
        api = AsyncMock()
        api.list_member_links.return_value = [
            MemberLink(discord_id=1, battlenet_linked=False),
            MemberLink(discord_id=2, battlenet_linked=False),
        ]
        cog = _make_cog(api=api, guilds=[guild])

        await cog._sweep_loop.coro(cog)

        api.put_member_profiles.assert_awaited_once()
        call_guild_id, profiles, departed_ids = api.put_member_profiles.await_args.args
        assert call_guild_id == 42
        assert [profile.discord_id for profile in profiles] == [1]
        assert departed_ids == [2]
        assert cog._linked_ids[42] == {1, 2}

    async def test_no_push_when_nothing_to_report(self) -> None:
        guild = _fake_guild()
        api = AsyncMock()
        api.list_member_links.return_value = []
        cog = _make_cog(api=api, guilds=[guild])

        await cog._sweep_loop.coro(cog)

        api.put_member_profiles.assert_not_awaited()


class TestSchedule:
    async def test_ids_scheduled_together_go_out_in_one_push(self) -> None:
        api = AsyncMock()
        guild = _fake_guild()
        guild.get_member.side_effect = {
            1: _fake_member(1, guild=guild),
            2: _fake_member(2, guild=guild),
        }.get
        cog = _make_cog(api=api, guilds=[guild])
        cog._linked_ids[guild.id] = {1, 2}

        cog._schedule(guild, 1)
        cog._schedule(guild, 2)
        await cog._debounce_tasks[guild.id]

        api.put_member_profiles.assert_awaited_once()
        _, profiles, departed_ids = api.put_member_profiles.await_args.args
        assert sorted(profile.discord_id for profile in profiles) == [1, 2]
        assert departed_ids == []

    async def test_push_404_does_not_stop_syncing_the_guild(self) -> None:
        """member-links already proved it's the site guild; a 404 on the push is an older API mid rolling deploy."""
        api = AsyncMock()
        api.put_member_profiles.side_effect = NotFoundError(404, "Not Found")
        guild = _fake_guild()
        guild.get_member.side_effect = {1: _fake_member(1, guild=guild)}.get
        cog = _make_cog(api=api, guilds=[guild])
        cog._linked_ids[guild.id] = {1}

        cog._schedule(guild, 1)
        await cog._debounce_tasks[guild.id]

        assert guild.id not in cog._not_site_guild_ids

    def test_skips_non_site_guilds(self) -> None:
        guild = _fake_guild()
        cog = _make_cog()
        cog._not_site_guild_ids.add(guild.id)

        cog._schedule(guild, 1)

        assert guild.id not in cog._pending_ids


class TestDebouncedPush:
    async def test_pushes_coalesced_ids(self) -> None:
        guild = _fake_guild(guild_id=42)
        member = _fake_member(1, guild=guild)
        guild.get_member.side_effect = {1: member}.get
        api = AsyncMock()
        cog = _make_cog(api=api)
        cog._linked_ids[42] = {1, 2}
        cog._pending_ids[42] = {1, 2}

        await cog._debounced_push(guild)

        api.put_member_profiles.assert_awaited_once()
        call_guild_id, profiles, departed_ids = api.put_member_profiles.await_args.args
        assert call_guild_id == 42
        assert [profile.discord_id for profile in profiles] == [1]
        assert departed_ids == [2]
        assert cog._pending_ids == {}

    async def test_no_push_when_nothing_pending(self) -> None:
        guild = _fake_guild()
        api = AsyncMock()
        cog = _make_cog(api=api)

        await cog._debounced_push(guild)

        api.put_member_profiles.assert_not_awaited()


class TestListeners:
    async def test_on_member_update_schedules_when_nick_changes(self) -> None:
        guild = _fake_guild()
        before = _fake_member(1, guild=guild, nick=None)
        after = _fake_member(1, guild=guild, nick="New Nick")
        cog = _make_cog()
        with patch.object(cog, "_schedule") as schedule:
            await MemberProfilesCog.on_member_update(cog, before, after)
        schedule.assert_called_once_with(guild, 1)

    async def test_on_member_update_schedules_when_roles_change(self) -> None:
        guild = _fake_guild()
        before = _fake_member(1, guild=guild, roles=[_fake_role(100)])
        after = _fake_member(1, guild=guild, roles=[_fake_role(100), _fake_role(200)])
        cog = _make_cog()
        with patch.object(cog, "_schedule") as schedule:
            await MemberProfilesCog.on_member_update(cog, before, after)
        schedule.assert_called_once_with(guild, 1)

    async def test_on_member_update_schedules_when_guild_avatar_changes(self) -> None:
        guild = _fake_guild()
        shared_role = _fake_role(100)
        before = _fake_member(1, guild=guild, roles=[shared_role], guild_avatar=None)
        after = _fake_member(1, guild=guild, roles=[shared_role], guild_avatar=_fake_asset("new"))
        cog = _make_cog()
        with patch.object(cog, "_schedule") as schedule:
            await MemberProfilesCog.on_member_update(cog, before, after)
        schedule.assert_called_once_with(guild, 1)

    async def test_on_member_update_ignores_unrelated_changes(self) -> None:
        guild = _fake_guild()
        shared_role = _fake_role(100)
        shared_avatar = _fake_asset("same")
        before = _fake_member(1, guild=guild, nick="Same", roles=[shared_role], guild_avatar=shared_avatar)
        after = _fake_member(1, guild=guild, nick="Same", roles=[shared_role], guild_avatar=shared_avatar)
        cog = _make_cog()
        with patch.object(cog, "_schedule") as schedule:
            await MemberProfilesCog.on_member_update(cog, before, after)
        schedule.assert_not_called()

    async def test_on_user_update_schedules_in_every_guild_the_user_is_in(self) -> None:
        guild_a = _fake_guild(guild_id=1)
        guild_a.get_member.side_effect = {5: MagicMock()}.get
        guild_b = _fake_guild(guild_id=2)
        guild_b.get_member.side_effect = {}.get  # user isn't in this guild
        before = Mock(spec=discord.User)
        before.id = 5
        before.name = "old-name"
        before.global_name = "Old"
        before.avatar = None
        after = Mock(spec=discord.User)
        after.id = 5
        after.name = "new-name"
        after.global_name = "Old"
        after.avatar = None
        cog = _make_cog(guilds=[guild_a, guild_b])
        with patch.object(cog, "_schedule") as schedule:
            await MemberProfilesCog.on_user_update(cog, before, after)
        schedule.assert_called_once_with(guild_a, 5)

    async def test_on_user_update_ignores_unrelated_changes(self) -> None:
        before = Mock(spec=discord.User)
        before.id, before.name, before.global_name, before.avatar = 5, "same", None, None
        after = Mock(spec=discord.User)
        after.id, after.name, after.global_name, after.avatar = 5, "same", None, None
        cog = _make_cog(guilds=[_fake_guild()])
        with patch.object(cog, "_schedule") as schedule:
            await MemberProfilesCog.on_user_update(cog, before, after)
        schedule.assert_not_called()

    async def test_on_member_join_schedules(self) -> None:
        guild = _fake_guild()
        member = _fake_member(1, guild=guild)
        cog = _make_cog()
        with patch.object(cog, "_schedule") as schedule:
            await MemberProfilesCog.on_member_join(cog, member)
        schedule.assert_called_once_with(guild, 1)

    async def test_on_member_remove_schedules(self) -> None:
        guild = _fake_guild()
        member = _fake_member(1, guild=guild)
        cog = _make_cog()
        with patch.object(cog, "_schedule") as schedule:
            await MemberProfilesCog.on_member_remove(cog, member)
        schedule.assert_called_once_with(guild, 1)
