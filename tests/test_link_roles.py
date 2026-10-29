"""Unit tests for the link-roles plugin: plan_role_changes, the sync loop/listeners, and the /linkroles group."""

import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch
from uuid import uuid4

import discord
import pytest

from sidequest_bot.api_client import APIError, APIResponseShapeError, NotFoundError
from sidequest_bot.plugins import link_roles as link_roles_module
from sidequest_bot.plugins.link_roles import LinkRolesCog, plan_role_changes, plan_role_strip
from sidequest_common.schemas import MemberLink, RoleResetCandidate


def _config(
    *,
    linked_role_id: int | None = 100,
    battlenet_role_id: int | None = 200,
    gate_role_id: int | None = None,
    roster_role_id: int | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        linked_role_id=linked_role_id,
        battlenet_role_id=battlenet_role_id,
        gate_role_id=gate_role_id,
        roster_role_id=roster_role_id,
    )


def _fake_role(role_id: int, *, assignable: bool = True, position: int = 0, managed: bool = False) -> MagicMock:
    role = MagicMock(spec=discord.Role)
    role.id = role_id
    role.is_default.return_value = False
    role.managed = managed
    role.position = position
    role.is_assignable.return_value = assignable
    role.permissions = discord.Permissions.none()
    role.mention = f"<@&{role_id}>"
    return role


def _fake_guild(*, guild_id: int = 42, roles: list[MagicMock] | None = None) -> MagicMock:
    guild = MagicMock(spec=discord.Guild)
    guild.id = guild_id
    roles_by_id = {role.id: role for role in (roles or [])}
    guild.get_role.side_effect = roles_by_id.get
    guild.members = []
    return guild


def _fake_member(
    member_id: int,
    *,
    bot: bool = False,
    pending: bool = False,
    role_ids: list[int] | None = None,
    roles: list[MagicMock] | None = None,
):
    member = MagicMock(spec=discord.Member)
    member.id = member_id
    member.bot = bot
    member.pending = pending
    member.roles = roles if roles is not None else [MagicMock(id=role_id) for role_id in (role_ids or [])]
    member.add_roles = AsyncMock()
    member.remove_roles = AsyncMock()
    member.send = AsyncMock()
    member.guild = SimpleNamespace(id=42, text_channels=[])
    return member


def _fake_reset(discord_id: int, *, reset_id=None, requested_at: datetime | None = None) -> RoleResetCandidate:
    return RoleResetCandidate(
        id=reset_id or uuid4(), discord_id=discord_id, requested_at=requested_at or datetime.now(UTC)
    )


def _make_cog(*, api: object | None = None, configs: object | None = None, guilds: list[object] | None = None):
    api = api or AsyncMock()
    if not isinstance(api.list_role_resets.return_value, list):
        api.list_role_resets.return_value = []
    cog = LinkRolesCog.__new__(LinkRolesCog)
    cog.bot = SimpleNamespace(api=api, configs=configs or AsyncMock(), guilds=guilds or [])
    cog._not_site_guild_ids = set()
    cog._warned_role_ids = set()
    return cog


def _make_interaction(*, guild_id: int = 1) -> Mock:
    interaction = Mock(spec=discord.Interaction)
    interaction.guild = Mock(spec=discord.Guild)
    interaction.guild.id = guild_id
    interaction.user = Mock(spec=discord.Member)
    interaction.response = Mock()
    interaction.response.send_message = AsyncMock()
    interaction.client = Mock()
    interaction.client.configs = Mock()
    interaction.client.configs.update = AsyncMock()
    return interaction


class TestPlanRoleChanges:
    """Pure: only ever touches the configured role ids, never anything else."""

    def test_adds_both_roles_for_new_fully_linked_member(self) -> None:
        to_add, to_remove = plan_role_changes(
            set(), linked=True, battlenet_linked=True, linked_role_id=1, battlenet_role_id=2, gate_role_id=None
        )
        assert sorted(to_add) == [1, 2]
        assert to_remove == []

    def test_adds_only_site_role_without_battlenet(self) -> None:
        to_add, to_remove = plan_role_changes(
            set(), linked=True, battlenet_linked=False, linked_role_id=1, battlenet_role_id=2, gate_role_id=None
        )
        assert to_add == [1]
        assert to_remove == []

    def test_removes_battlenet_role_on_unlink(self) -> None:
        to_add, to_remove = plan_role_changes(
            {1, 2}, linked=True, battlenet_linked=False, linked_role_id=1, battlenet_role_id=2, gate_role_id=None
        )
        assert to_add == []
        assert to_remove == [2]

    def test_removes_both_roles_when_no_longer_a_site_member(self) -> None:
        to_add, to_remove = plan_role_changes(
            {1, 2}, linked=False, battlenet_linked=False, linked_role_id=1, battlenet_role_id=2, gate_role_id=None
        )
        assert to_add == []
        assert sorted(to_remove) == [1, 2]

    def test_never_touches_other_roles(self) -> None:
        to_add, to_remove = plan_role_changes(
            {999}, linked=True, battlenet_linked=True, linked_role_id=1, battlenet_role_id=2, gate_role_id=None
        )
        assert sorted(to_add) == [1, 2]
        assert 999 not in to_add
        assert 999 not in to_remove

    def test_none_role_ids_are_ignored(self) -> None:
        to_add, to_remove = plan_role_changes(
            set(), linked=True, battlenet_linked=True, linked_role_id=None, battlenet_role_id=None, gate_role_id=None
        )
        assert to_add == []
        assert to_remove == []

    def test_no_change_when_already_correct(self) -> None:
        to_add, to_remove = plan_role_changes(
            {1}, linked=True, battlenet_linked=False, linked_role_id=1, battlenet_role_id=2, gate_role_id=None
        )
        assert to_add == []
        assert to_remove == []

    def test_adds_gate_role_for_unlinked_member(self) -> None:
        to_add, to_remove = plan_role_changes(
            set(), linked=False, battlenet_linked=False, linked_role_id=1, battlenet_role_id=2, gate_role_id=3
        )
        assert 3 in to_add
        assert 3 not in to_remove

    def test_removes_gate_role_once_linked(self) -> None:
        to_add, to_remove = plan_role_changes(
            {3}, linked=True, battlenet_linked=False, linked_role_id=1, battlenet_role_id=2, gate_role_id=3
        )
        assert 3 not in to_add
        assert 3 in to_remove

    def test_gate_role_untouched_when_already_correct(self) -> None:
        to_add, to_remove = plan_role_changes(
            {3}, linked=False, battlenet_linked=False, linked_role_id=1, battlenet_role_id=2, gate_role_id=3
        )
        assert to_add == []
        assert to_remove == []

    def test_gate_role_none_is_ignored(self) -> None:
        to_add, to_remove = plan_role_changes(
            set(), linked=False, battlenet_linked=False, linked_role_id=None, battlenet_role_id=None, gate_role_id=None
        )
        assert to_add == []
        assert to_remove == []

    def test_grants_roster_role_when_linked_and_confirmed(self) -> None:
        to_add, to_remove = plan_role_changes(
            set(),
            linked=True,
            battlenet_linked=False,
            linked_role_id=1,
            battlenet_role_id=2,
            gate_role_id=None,
            roster_role_id=3,
            roster_confirmed=True,
            ranked_above_roster_role=False,
        )
        assert 3 in to_add
        assert 3 not in to_remove

    def test_no_roster_role_when_not_confirmed(self) -> None:
        to_add, to_remove = plan_role_changes(
            set(),
            linked=True,
            battlenet_linked=False,
            linked_role_id=1,
            battlenet_role_id=2,
            gate_role_id=None,
            roster_role_id=3,
            roster_confirmed=False,
            ranked_above_roster_role=False,
        )
        assert 3 not in to_add
        assert to_remove == []

    def test_no_re_grant_of_roster_role_when_ranked_above_it(self) -> None:
        to_add, to_remove = plan_role_changes(
            set(),
            linked=True,
            battlenet_linked=False,
            linked_role_id=1,
            battlenet_role_id=2,
            gate_role_id=None,
            roster_role_id=3,
            roster_confirmed=True,
            ranked_above_roster_role=True,
        )
        assert 3 not in to_add
        assert to_remove == []

    def test_removes_roster_role_once_unconfirmed(self) -> None:
        to_add, to_remove = plan_role_changes(
            {3},
            linked=True,
            battlenet_linked=False,
            linked_role_id=1,
            battlenet_role_id=2,
            gate_role_id=None,
            roster_role_id=3,
            roster_confirmed=False,
            ranked_above_roster_role=False,
        )
        assert 3 not in to_add
        assert 3 in to_remove

    def test_roster_role_none_is_ignored(self) -> None:
        to_add, to_remove = plan_role_changes(
            set(),
            linked=True,
            battlenet_linked=False,
            linked_role_id=None,
            battlenet_role_id=None,
            gate_role_id=None,
            roster_role_id=None,
            roster_confirmed=True,
            ranked_above_roster_role=False,
        )
        assert to_add == []
        assert to_remove == []


class TestPlanRoleStrip:
    """Pure: only touches roles the bot can manage, and never the gate role."""

    def test_removes_every_assignable_role(self) -> None:
        roles = [_fake_role(1), _fake_role(2)]
        assert sorted(plan_role_strip(roles, gate_role_id=None)) == [1, 2]

    def test_keeps_everyone_managed_and_above_bot_roles(self) -> None:
        """Roles the bot can't manage report ``is_assignable() == False`` and must never be touched."""
        roles = [_fake_role(1, assignable=False), _fake_role(2)]
        assert plan_role_strip(roles, gate_role_id=None) == [2]

    def test_keeps_gate_role_out_of_the_removal_list(self) -> None:
        gate_role = _fake_role(300)
        other_role = _fake_role(100)
        assert sorted(plan_role_strip([gate_role, other_role], gate_role_id=300)) == [100]

    def test_no_gate_role_configured_strips_everything_assignable(self) -> None:
        roles = [_fake_role(1), _fake_role(2), _fake_role(3, assignable=False)]
        assert sorted(plan_role_strip(roles, gate_role_id=None)) == [1, 2]

    def test_empty_roles_returns_empty(self) -> None:
        assert plan_role_strip([], gate_role_id=300) == []


class TestSyncGuild:
    async def test_skips_when_both_role_ids_unset(self) -> None:
        configs = AsyncMock()
        configs.get.return_value = _config(linked_role_id=None, battlenet_role_id=None)
        api = AsyncMock()
        cog = _make_cog(api=api, configs=configs)

        await cog._sync_guild(_fake_guild())

        api.list_member_links.assert_not_awaited()

    async def test_no_discord_call_when_nothing_changes(self) -> None:
        role = _fake_role(100)
        guild = _fake_guild(roles=[role])
        member = _fake_member(1, role_ids=[100])
        guild.members = [member]
        configs = AsyncMock()
        configs.get.return_value = _config(linked_role_id=100, battlenet_role_id=None)
        api = AsyncMock()
        api.list_member_links.return_value = [MemberLink(discord_id=1, battlenet_linked=False)]
        cog = _make_cog(api=api, configs=configs)

        await cog._sync_guild(guild)

        member.add_roles.assert_not_awaited()
        member.remove_roles.assert_not_awaited()

    async def test_grants_configured_roles_to_a_fully_linked_member(self) -> None:
        site_role = _fake_role(100)
        bnet_role = _fake_role(200)
        guild = _fake_guild(roles=[site_role, bnet_role])
        member = _fake_member(1, role_ids=[])
        guild.members = [member]
        configs = AsyncMock()
        configs.get.return_value = _config(linked_role_id=100, battlenet_role_id=200)
        api = AsyncMock()
        api.list_member_links.return_value = [MemberLink(discord_id=1, battlenet_linked=True)]
        cog = _make_cog(api=api, configs=configs)

        await cog._sync_guild(guild)

        member.add_roles.assert_any_await(site_role, reason="Signed in on sidequest-guild.com")
        member.add_roles.assert_any_await(bnet_role, reason="Linked Battle.net")
        member.remove_roles.assert_not_awaited()

    async def test_grants_gate_role_to_unlinked_member(self) -> None:
        gate_role = _fake_role(300)
        guild = _fake_guild(roles=[gate_role])
        member = _fake_member(1, role_ids=[])
        guild.members = [member]
        configs = AsyncMock()
        configs.get.return_value = _config(linked_role_id=None, battlenet_role_id=None, gate_role_id=300)
        api = AsyncMock()
        api.list_member_links.return_value = []
        cog = _make_cog(api=api, configs=configs)

        await cog._sync_guild(guild)

        member.add_roles.assert_any_await(gate_role, reason="Hasn't signed in on sidequest-guild.com yet")
        member.remove_roles.assert_not_awaited()

    async def test_removes_gate_role_when_member_signs_in(self) -> None:
        gate_role = _fake_role(300)
        guild = _fake_guild(roles=[gate_role])
        member = _fake_member(1, role_ids=[300])
        guild.members = [member]
        configs = AsyncMock()
        configs.get.return_value = _config(linked_role_id=None, battlenet_role_id=None, gate_role_id=300)
        api = AsyncMock()
        api.list_member_links.return_value = [MemberLink(discord_id=1, battlenet_linked=False)]
        cog = _make_cog(api=api, configs=configs)

        await cog._sync_guild(guild)

        member.add_roles.assert_not_awaited()
        member.remove_roles.assert_any_await(gate_role, reason="Link roles sync")

    async def test_gate_role_untouched_when_not_configured(self) -> None:
        site_role = _fake_role(100)
        guild = _fake_guild(roles=[site_role])
        member = _fake_member(1, role_ids=[])
        guild.members = [member]
        configs = AsyncMock()
        configs.get.return_value = _config(linked_role_id=100, battlenet_role_id=None, gate_role_id=None)
        api = AsyncMock()
        api.list_member_links.return_value = []
        cog = _make_cog(api=api, configs=configs)

        await cog._sync_guild(guild)

        member.add_roles.assert_not_awaited()
        member.remove_roles.assert_not_awaited()

    async def test_bot_members_are_skipped(self) -> None:
        site_role = _fake_role(100)
        guild = _fake_guild(roles=[site_role])
        member = _fake_member(1, bot=True, role_ids=[])
        guild.members = [member]
        configs = AsyncMock()
        configs.get.return_value = _config(linked_role_id=100, battlenet_role_id=None)
        api = AsyncMock()
        api.list_member_links.return_value = [MemberLink(discord_id=1, battlenet_linked=False)]
        cog = _make_cog(api=api, configs=configs)

        await cog._sync_guild(guild)

        member.add_roles.assert_not_awaited()

    async def test_blocked_role_warns_once_and_is_skipped(self, caplog: pytest.LogCaptureFixture) -> None:
        role = _fake_role(100, assignable=False)
        guild = _fake_guild(roles=[role])
        member = _fake_member(1, role_ids=[])
        guild.members = [member]
        configs = AsyncMock()
        configs.get.return_value = _config(linked_role_id=100, battlenet_role_id=None)
        api = AsyncMock()
        api.list_member_links.return_value = [MemberLink(discord_id=1, battlenet_linked=False)]
        cog = _make_cog(api=api, configs=configs)

        with caplog.at_level(logging.WARNING):
            await cog._sync_guild(guild)
            await cog._sync_guild(guild)

        member.add_roles.assert_not_awaited()
        warnings = [r for r in caplog.records if "no longer safe to grant" in r.message]
        assert len(warnings) == 1

    async def test_missing_role_warns_once_and_is_skipped(self, caplog: pytest.LogCaptureFixture) -> None:
        guild = _fake_guild(roles=[])
        member = _fake_member(1, role_ids=[])
        guild.members = [member]
        configs = AsyncMock()
        configs.get.return_value = _config(linked_role_id=100, battlenet_role_id=None)
        api = AsyncMock()
        api.list_member_links.return_value = [MemberLink(discord_id=1, battlenet_linked=False)]
        cog = _make_cog(api=api, configs=configs)

        with caplog.at_level(logging.WARNING):
            await cog._sync_guild(guild)
            await cog._sync_guild(guild)

        member.add_roles.assert_not_awaited()
        warnings = [r for r in caplog.records if "no longer exists" in r.message]
        assert len(warnings) == 1

    async def test_grants_roster_role_to_confirmed_linked_member(self) -> None:
        roster_role = _fake_role(300)
        guild = _fake_guild(roles=[roster_role])
        member = _fake_member(1, role_ids=[])
        guild.members = [member]
        configs = AsyncMock()
        configs.get.return_value = _config(linked_role_id=None, battlenet_role_id=None, roster_role_id=300)
        api = AsyncMock()
        api.list_member_links.return_value = [MemberLink(discord_id=1, battlenet_linked=False, roster_confirmed=True)]
        cog = _make_cog(api=api, configs=configs)

        await cog._sync_guild(guild)

        member.add_roles.assert_any_await(roster_role, reason="Confirmed on the guild roster")
        member.remove_roles.assert_not_awaited()

    async def test_removes_roster_role_from_unconfirmed_member(self) -> None:
        roster_role = _fake_role(300)
        guild = _fake_guild(roles=[roster_role])
        member = _fake_member(1, role_ids=[300])
        guild.members = [member]
        configs = AsyncMock()
        configs.get.return_value = _config(linked_role_id=None, battlenet_role_id=None, roster_role_id=300)
        api = AsyncMock()
        api.list_member_links.return_value = [MemberLink(discord_id=1, battlenet_linked=False, roster_confirmed=False)]
        cog = _make_cog(api=api, configs=configs)

        await cog._sync_guild(guild)

        member.add_roles.assert_not_awaited()
        member.remove_roles.assert_any_await(roster_role, reason="Link roles sync")

    async def test_roster_role_not_re_granted_to_a_member_ranked_above_it(self) -> None:
        roster_role = _fake_role(300, position=5)
        officer_role = _fake_role(301, position=10)
        guild = _fake_guild(roles=[roster_role, officer_role])
        member = _fake_member(1, roles=[officer_role])
        guild.members = [member]
        configs = AsyncMock()
        configs.get.return_value = _config(linked_role_id=None, battlenet_role_id=None, roster_role_id=300)
        api = AsyncMock()
        api.list_member_links.return_value = [MemberLink(discord_id=1, battlenet_linked=False, roster_confirmed=True)]
        cog = _make_cog(api=api, configs=configs)

        await cog._sync_guild(guild)

        member.add_roles.assert_not_awaited()
        member.remove_roles.assert_not_awaited()

    async def test_roster_role_untouched_when_not_configured(self) -> None:
        guild = _fake_guild(roles=[])
        member = _fake_member(1, role_ids=[])
        guild.members = [member]
        configs = AsyncMock()
        configs.get.return_value = _config(linked_role_id=None, battlenet_role_id=None, roster_role_id=None)
        api = AsyncMock()
        api.list_member_links.return_value = [MemberLink(discord_id=1, battlenet_linked=False, roster_confirmed=True)]
        cog = _make_cog(api=api, configs=configs)

        await cog._sync_guild(guild)

        member.add_roles.assert_not_awaited()
        member.remove_roles.assert_not_awaited()


class TestGuestWelcomeDm:
    """The gate -> guest transition (granting linked_role_id) DMs a one-time welcome."""

    async def test_sends_dm_once_on_grant_not_on_later_tick(self) -> None:
        site_role = _fake_role(100)
        guild = _fake_guild(roles=[site_role])
        member = _fake_member(1, role_ids=[])
        guild.members = [member]
        configs = AsyncMock()
        configs.get.return_value = _config(linked_role_id=100, battlenet_role_id=None)
        api = AsyncMock()
        api.list_member_links.return_value = [MemberLink(discord_id=1, battlenet_linked=False)]
        cog = _make_cog(api=api, configs=configs)

        with patch.object(link_roles_module, "send_guest_welcome_dm", new_callable=AsyncMock) as send_dm:
            await cog._sync_guild(guild)
            send_dm.assert_awaited_once_with(member)

            # Discord would now actually hold the role, so the next tick's member_role_ids reflects that and
            # plan_role_changes no longer lists it in to_add - no stored "already welcomed" flag needed.
            member.roles = [site_role]
            await cog._sync_guild(guild)
            assert send_dm.await_count == 1

    async def test_not_sent_for_other_role_grants(self) -> None:
        bnet_role = _fake_role(200)
        guild = _fake_guild(roles=[bnet_role])
        member = _fake_member(1, role_ids=[])
        guild.members = [member]
        configs = AsyncMock()
        configs.get.return_value = _config(linked_role_id=None, battlenet_role_id=200)
        api = AsyncMock()
        api.list_member_links.return_value = [MemberLink(discord_id=1, battlenet_linked=True)]
        cog = _make_cog(api=api, configs=configs)

        with patch.object(link_roles_module, "send_guest_welcome_dm", new_callable=AsyncMock) as send_dm:
            await cog._sync_guild(guild)
            send_dm.assert_not_awaited()

    async def test_forbidden_dm_does_not_stop_role_change(self) -> None:
        site_role = _fake_role(100)
        bnet_role = _fake_role(200)
        guild = _fake_guild(roles=[site_role, bnet_role])
        member = _fake_member(1, role_ids=[])
        member.send = AsyncMock(side_effect=discord.Forbidden(MagicMock(status=403), "Cannot send messages"))
        guild.members = [member]
        configs = AsyncMock()
        configs.get.return_value = _config(linked_role_id=100, battlenet_role_id=200)
        api = AsyncMock()
        api.list_member_links.return_value = [MemberLink(discord_id=1, battlenet_linked=True)]
        cog = _make_cog(api=api, configs=configs)

        await cog._sync_guild(guild)

        member.add_roles.assert_any_await(site_role, reason="Signed in on sidequest-guild.com")
        member.add_roles.assert_any_await(bnet_role, reason="Linked Battle.net")
        member.remove_roles.assert_not_awaited()


class TestSyncLoop:
    async def test_not_found_marks_guild_non_site(self) -> None:
        guild = _fake_guild()
        configs = AsyncMock()
        configs.get.side_effect = NotFoundError(404, "not the site guild")
        cog = _make_cog(configs=configs, guilds=[guild])

        await cog._sync_loop.coro(cog)
        await cog._sync_loop.coro(cog)

        assert guild.id in cog._not_site_guild_ids
        assert configs.get.await_count == 1

    @pytest.mark.parametrize(
        "error",
        [
            APIError(500, "boom"),
            # What production hit during a rolling deploy: the bot updated before the API, whose config response
            # didn't match the bot's schema yet. Escaping here stops the tasks.loop for good.
            pytest.param(APIResponseShapeError(502, "unexpected response shape"), id="response-shape-mismatch"),
        ],
    )
    async def test_failed_fetch_does_not_crash_the_loop(self, error: APIError) -> None:
        guild = _fake_guild()
        configs = AsyncMock()
        configs.get.side_effect = error
        cog = _make_cog(configs=configs, guilds=[guild])

        await cog._sync_loop.coro(cog)  # must not raise

        assert guild.id not in cog._not_site_guild_ids

    async def test_role_resets_404_mid_deploy_keeps_link_roles_running(self) -> None:
        """New bot, old API: member-links answers but role-resets 404s. That must not switch the guild off."""
        guild = _fake_guild()
        configs = AsyncMock()
        configs.get.return_value = _config(gate_role_id=300)
        api = AsyncMock()
        api.list_member_links.return_value = []
        api.list_role_resets.side_effect = NotFoundError(404, "Not Found")
        cog = _make_cog(api=api, configs=configs, guilds=[guild])

        await cog._sync_loop.coro(cog)
        await cog._sync_loop.coro(cog)

        assert guild.id not in cog._not_site_guild_ids
        assert api.list_role_resets.await_count == 2


class TestListeners:
    async def test_on_member_join_skips_bot_members(self) -> None:
        cog = _make_cog()
        member = _fake_member(1, bot=True)
        with patch.object(cog, "_sync_member_now", AsyncMock()) as sync:
            await LinkRolesCog.on_member_join(cog, member)
        sync.assert_not_awaited()

    async def test_on_member_join_skips_pending_members(self) -> None:
        cog = _make_cog()
        member = _fake_member(1, pending=True)
        with patch.object(cog, "_sync_member_now", AsyncMock()) as sync:
            await LinkRolesCog.on_member_join(cog, member)
        sync.assert_not_awaited()

    async def test_on_member_join_syncs_a_non_pending_member(self) -> None:
        cog = _make_cog()
        member = _fake_member(1, pending=False)
        with patch.object(cog, "_sync_member_now", AsyncMock()) as sync:
            await LinkRolesCog.on_member_join(cog, member)
        sync.assert_awaited_once_with(member)

    async def test_on_member_update_syncs_once_screening_clears(self) -> None:
        cog = _make_cog()
        before = _fake_member(1, pending=True)
        after = _fake_member(1, pending=False)
        with patch.object(cog, "_sync_member_now", AsyncMock()) as sync:
            await LinkRolesCog.on_member_update(cog, before, after)
        sync.assert_awaited_once_with(after)

    async def test_on_member_update_ignores_members_still_pending(self) -> None:
        cog = _make_cog()
        before = _fake_member(1, pending=True)
        after = _fake_member(1, pending=True)
        with patch.object(cog, "_sync_member_now", AsyncMock()) as sync:
            await LinkRolesCog.on_member_update(cog, before, after)
        sync.assert_not_awaited()

    async def test_sync_member_now_marks_guild_non_site_on_404(self) -> None:
        guild = _fake_guild()
        member = _fake_member(1)
        member.guild = guild
        configs = AsyncMock()
        configs.get.side_effect = NotFoundError(404, "not the site guild")
        cog = _make_cog(configs=configs)

        await cog._sync_member_now(member)

        assert guild.id in cog._not_site_guild_ids


class TestLinkrolesGroup:
    async def test_site_blocks_a_role_the_invoker_may_not_configure(self) -> None:
        cog = _make_cog()
        interaction = _make_interaction()
        role = Mock(spec=discord.Role)
        with patch.object(link_roles_module, "autorole_block_reason", return_value="above your rank"):
            await LinkRolesCog.linkroles_site.callback(cog, interaction, role)
        interaction.response.send_message.assert_awaited_once_with("above your rank", ephemeral=True)
        interaction.client.configs.update.assert_not_awaited()

    async def test_site_sets_linked_role_id_when_allowed(self) -> None:
        cog = _make_cog()
        interaction = _make_interaction()
        role = Mock(spec=discord.Role)
        role.id = 100
        role.mention = "<@&100>"
        with patch.object(link_roles_module, "autorole_block_reason", return_value=None):
            await LinkRolesCog.linkroles_site.callback(cog, interaction, role)
        update_call = interaction.client.configs.update.await_args
        assert update_call.args[0] == interaction.guild.id
        assert update_call.args[1].linked_role_id == 100
        interaction.response.send_message.assert_awaited_once()

    async def test_site_clears_linked_role_id_when_role_omitted(self) -> None:
        cog = _make_cog()
        interaction = _make_interaction()
        await LinkRolesCog.linkroles_site.callback(cog, interaction, None)
        update_call = interaction.client.configs.update.await_args
        assert update_call.args[1].linked_role_id is None

    async def test_battlenet_blocks_a_role_the_invoker_may_not_configure(self) -> None:
        cog = _make_cog()
        interaction = _make_interaction()
        role = Mock(spec=discord.Role)
        with patch.object(link_roles_module, "autorole_block_reason", return_value="above your rank"):
            await LinkRolesCog.linkroles_battlenet.callback(cog, interaction, role)
        interaction.response.send_message.assert_awaited_once_with("above your rank", ephemeral=True)
        interaction.client.configs.update.assert_not_awaited()

    async def test_battlenet_sets_battlenet_role_id_when_allowed(self) -> None:
        cog = _make_cog()
        interaction = _make_interaction()
        role = Mock(spec=discord.Role)
        role.id = 200
        role.mention = "<@&200>"
        with patch.object(link_roles_module, "autorole_block_reason", return_value=None):
            await LinkRolesCog.linkroles_battlenet.callback(cog, interaction, role)
        update_call = interaction.client.configs.update.await_args
        assert update_call.args[1].battlenet_role_id == 200

    async def test_gate_blocks_a_role_the_invoker_may_not_configure(self) -> None:
        cog = _make_cog()
        interaction = _make_interaction()
        role = Mock(spec=discord.Role)
        with patch.object(link_roles_module, "autorole_block_reason", return_value="above your rank"):
            await LinkRolesCog.linkroles_gate.callback(cog, interaction, role)
        interaction.response.send_message.assert_awaited_once_with("above your rank", ephemeral=True)
        interaction.client.configs.update.assert_not_awaited()

    async def test_gate_sets_gate_role_id_when_allowed(self) -> None:
        cog = _make_cog()
        interaction = _make_interaction()
        role = Mock(spec=discord.Role)
        role.id = 300
        role.mention = "<@&300>"
        with patch.object(link_roles_module, "autorole_block_reason", return_value=None):
            await LinkRolesCog.linkroles_gate.callback(cog, interaction, role)
        update_call = interaction.client.configs.update.await_args
        assert update_call.args[1].gate_role_id == 300

    async def test_gate_clears_gate_role_id_when_role_omitted(self) -> None:
        cog = _make_cog()
        interaction = _make_interaction()
        await LinkRolesCog.linkroles_gate.callback(cog, interaction, None)
        update_call = interaction.client.configs.update.await_args
        assert update_call.args[1].gate_role_id is None

    async def test_roster_blocks_a_role_the_invoker_may_not_configure(self) -> None:
        cog = _make_cog()
        interaction = _make_interaction()
        role = Mock(spec=discord.Role)
        with patch.object(link_roles_module, "autorole_block_reason", return_value="above your rank"):
            await LinkRolesCog.linkroles_roster.callback(cog, interaction, role)
        interaction.response.send_message.assert_awaited_once_with("above your rank", ephemeral=True)
        interaction.client.configs.update.assert_not_awaited()

    async def test_roster_sets_roster_role_id_when_allowed(self) -> None:
        cog = _make_cog()
        interaction = _make_interaction()
        role = Mock(spec=discord.Role)
        role.id = 400
        role.mention = "<@&400>"
        with patch.object(link_roles_module, "autorole_block_reason", return_value=None):
            await LinkRolesCog.linkroles_roster.callback(cog, interaction, role)
        update_call = interaction.client.configs.update.await_args
        assert update_call.args[1].roster_role_id == 400

    async def test_roster_clears_roster_role_id_when_role_omitted(self) -> None:
        cog = _make_cog()
        interaction = _make_interaction()
        await LinkRolesCog.linkroles_roster.callback(cog, interaction, None)
        update_call = interaction.client.configs.update.await_args
        assert update_call.args[1].roster_role_id is None


class TestProcessRoleResets:
    """Role resets owed after a site account deletion: strip manageable roles, re-gate, ack."""

    async def test_no_pending_resets_does_nothing(self) -> None:
        api = AsyncMock()
        api.list_role_resets.return_value = []
        cog = _make_cog(api=api)
        guild = _fake_guild()

        await cog._process_role_resets(guild, _config())

        api.ack_role_reset.assert_not_awaited()

    async def test_strips_manageable_roles_and_grants_gate_role(self) -> None:
        gate_role = _fake_role(300)
        kept_role = _fake_role(400, assignable=False)
        stripped_role = _fake_role(100)
        guild = _fake_guild(roles=[gate_role, kept_role, stripped_role])
        member = _fake_member(1, roles=[stripped_role, kept_role])
        guild.get_member = Mock(return_value=member)
        reset = _fake_reset(1)
        api = AsyncMock()
        api.list_role_resets.return_value = [reset]
        cog = _make_cog(api=api)

        await cog._process_role_resets(guild, _config(gate_role_id=300))

        member.remove_roles.assert_awaited_once_with(stripped_role, reason="Site account deleted")
        member.add_roles.assert_awaited_once_with(gate_role, reason="Site account deleted")
        api.ack_role_reset.assert_awaited_once_with(guild.id, reset.id)

    async def test_member_already_gone_is_acked_without_role_changes(self) -> None:
        guild = _fake_guild()
        guild.get_member = Mock(return_value=None)
        reset = _fake_reset(1)
        api = AsyncMock()
        api.list_role_resets.return_value = [reset]
        cog = _make_cog(api=api)

        await cog._process_role_resets(guild, _config(gate_role_id=None))

        api.ack_role_reset.assert_awaited_once_with(guild.id, reset.id)

    async def test_discord_error_on_removal_skips_ack(self) -> None:
        stripped_role = _fake_role(100)
        guild = _fake_guild(roles=[stripped_role])
        member = _fake_member(1, roles=[stripped_role])
        member.remove_roles.side_effect = discord.HTTPException(MagicMock(status=500), "boom")
        guild.get_member = Mock(return_value=member)
        reset = _fake_reset(1)
        api = AsyncMock()
        api.list_role_resets.return_value = [reset]
        cog = _make_cog(api=api)

        await cog._process_role_resets(guild, _config(gate_role_id=None))

        api.ack_role_reset.assert_not_awaited()

    async def test_gate_role_missing_still_strips_but_skips_grant(self) -> None:
        stripped_role = _fake_role(100)
        guild = _fake_guild(roles=[stripped_role])  # gate role 300 is not registered on the guild
        member = _fake_member(1, roles=[stripped_role])
        guild.get_member = Mock(return_value=member)
        reset = _fake_reset(1)
        api = AsyncMock()
        api.list_role_resets.return_value = [reset]
        cog = _make_cog(api=api)

        await cog._process_role_resets(guild, _config(gate_role_id=300))

        member.remove_roles.assert_awaited_once_with(stripped_role, reason="Site account deleted")
        member.add_roles.assert_not_awaited()
        api.ack_role_reset.assert_awaited_once_with(guild.id, reset.id)

    async def test_ack_failure_is_caught_not_raised(self) -> None:
        guild = _fake_guild()
        guild.get_member = Mock(return_value=None)
        reset = _fake_reset(1)
        api = AsyncMock()
        api.list_role_resets.return_value = [reset]
        api.ack_role_reset.side_effect = APIError(500, "boom")
        cog = _make_cog(api=api)

        await cog._process_role_resets(guild, _config(gate_role_id=None))
