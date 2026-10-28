"""Unit tests for the link-roles plugin: plan_role_changes, the sync loop/listeners, and the /linkroles group."""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import discord
import pytest

from sidequest_bot.api_client import APIError, NotFoundError
from sidequest_bot.plugins import link_roles as link_roles_module
from sidequest_bot.plugins.link_roles import LinkRolesCog, plan_role_changes
from sidequest_common.schemas import MemberLink


def _config(*, linked_role_id: int | None = 100, battlenet_role_id: int | None = 200) -> SimpleNamespace:
    return SimpleNamespace(linked_role_id=linked_role_id, battlenet_role_id=battlenet_role_id)


def _fake_role(role_id: int, *, assignable: bool = True) -> MagicMock:
    role = MagicMock(spec=discord.Role)
    role.id = role_id
    role.is_default.return_value = False
    role.managed = False
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


def _fake_member(member_id: int, *, bot: bool = False, pending: bool = False, role_ids: list[int] | None = None):
    member = MagicMock(spec=discord.Member)
    member.id = member_id
    member.bot = bot
    member.pending = pending
    member.roles = [MagicMock(id=role_id) for role_id in (role_ids or [])]
    member.add_roles = AsyncMock()
    member.remove_roles = AsyncMock()
    return member


def _make_cog(*, api: object | None = None, configs: object | None = None, guilds: list[object] | None = None):
    cog = LinkRolesCog.__new__(LinkRolesCog)
    cog.bot = SimpleNamespace(api=api or AsyncMock(), configs=configs or AsyncMock(), guilds=guilds or [])
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
    """Pure: only ever touches the two configured role ids, never anything else."""

    def test_adds_both_roles_for_new_fully_linked_member(self) -> None:
        to_add, to_remove = plan_role_changes(
            set(), linked=True, battlenet_linked=True, linked_role_id=1, battlenet_role_id=2
        )
        assert sorted(to_add) == [1, 2]
        assert to_remove == []

    def test_adds_only_site_role_without_battlenet(self) -> None:
        to_add, to_remove = plan_role_changes(
            set(), linked=True, battlenet_linked=False, linked_role_id=1, battlenet_role_id=2
        )
        assert to_add == [1]
        assert to_remove == []

    def test_removes_battlenet_role_on_unlink(self) -> None:
        to_add, to_remove = plan_role_changes(
            {1, 2}, linked=True, battlenet_linked=False, linked_role_id=1, battlenet_role_id=2
        )
        assert to_add == []
        assert to_remove == [2]

    def test_removes_both_roles_when_no_longer_a_site_member(self) -> None:
        to_add, to_remove = plan_role_changes(
            {1, 2}, linked=False, battlenet_linked=False, linked_role_id=1, battlenet_role_id=2
        )
        assert to_add == []
        assert sorted(to_remove) == [1, 2]

    def test_never_touches_other_roles(self) -> None:
        to_add, to_remove = plan_role_changes(
            {999}, linked=True, battlenet_linked=True, linked_role_id=1, battlenet_role_id=2
        )
        assert sorted(to_add) == [1, 2]
        assert 999 not in to_add
        assert 999 not in to_remove

    def test_none_role_ids_are_ignored(self) -> None:
        to_add, to_remove = plan_role_changes(
            set(), linked=True, battlenet_linked=True, linked_role_id=None, battlenet_role_id=None
        )
        assert to_add == []
        assert to_remove == []

    def test_no_change_when_already_correct(self) -> None:
        to_add, to_remove = plan_role_changes(
            {1}, linked=True, battlenet_linked=False, linked_role_id=1, battlenet_role_id=2
        )
        assert to_add == []
        assert to_remove == []


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

    async def test_other_api_errors_do_not_crash_the_loop(self) -> None:
        guild = _fake_guild()
        configs = AsyncMock()
        configs.get.side_effect = APIError(500, "boom")
        cog = _make_cog(configs=configs, guilds=[guild])

        await cog._sync_loop.coro(cog)  # must not raise

        assert guild.id not in cog._not_site_guild_ids


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
