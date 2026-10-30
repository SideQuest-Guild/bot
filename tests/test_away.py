"""Unit tests for the away plugin: plan_away_role, the sync loop, and the ``/away role`` command."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import discord
import pytest

from sidequest_bot.api_client import APIError, NotFoundError
from sidequest_bot.plugins import away as away_module
from sidequest_bot.plugins.away import AwayCog, plan_away_role


def _config(*, away_role_id: int | None = 400) -> SimpleNamespace:
    return SimpleNamespace(away_role_id=away_role_id)


def _fake_role(role_id: int, *, assignable: bool = True, managed: bool = False) -> MagicMock:
    role = MagicMock(spec=discord.Role)
    role.id = role_id
    role.is_default.return_value = False
    role.managed = managed
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


def _fake_member(member_id: int, *, bot: bool = False, role_ids: list[int] | None = None) -> MagicMock:
    member = MagicMock(spec=discord.Member)
    member.id = member_id
    member.bot = bot
    member.roles = [MagicMock(id=role_id) for role_id in (role_ids or [])]
    member.add_roles = AsyncMock()
    member.remove_roles = AsyncMock()
    member.guild = SimpleNamespace(id=42)
    return member


def _make_cog(*, api: object | None = None, configs: object | None = None, guilds: list[object] | None = None):
    cog = AwayCog.__new__(AwayCog)
    cog.bot = SimpleNamespace(api=api or AsyncMock(), configs=configs or AsyncMock(), guilds=guilds or [])
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


class TestPlanAwayRole:
    """Pure: grant while away and not yet granted, remove once no longer away and still held."""

    def test_grants_when_away_and_unheld(self) -> None:
        assert plan_away_role(away_now=True, has_role=False) == (True, False)

    def test_no_op_when_away_and_already_held(self) -> None:
        assert plan_away_role(away_now=True, has_role=True) == (False, False)

    def test_removes_when_not_away_and_held(self) -> None:
        assert plan_away_role(away_now=False, has_role=True) == (False, True)

    def test_no_op_when_not_away_and_unheld(self) -> None:
        assert plan_away_role(away_now=False, has_role=False) == (False, False)


class TestSyncGuild:
    async def test_skips_when_away_role_unset(self) -> None:
        configs = AsyncMock()
        configs.get.return_value = _config(away_role_id=None)
        api = AsyncMock()
        cog = _make_cog(api=api, configs=configs)

        await cog._sync_guild(_fake_guild())

        api.get_away_now.assert_not_awaited()

    async def test_grants_the_role_to_a_member_away_now(self) -> None:
        role = _fake_role(400)
        guild = _fake_guild(roles=[role])
        member = _fake_member(1, role_ids=[])
        guild.members = [member]
        configs = AsyncMock()
        configs.get.return_value = _config(away_role_id=400)
        api = AsyncMock()
        api.get_away_now.return_value = [1]
        cog = _make_cog(api=api, configs=configs)

        await cog._sync_guild(guild)

        member.add_roles.assert_awaited_once_with(role, reason="Marked away on sidequest-guild.com")
        member.remove_roles.assert_not_awaited()

    async def test_removes_the_role_once_no_longer_away(self) -> None:
        role = _fake_role(400)
        guild = _fake_guild(roles=[role])
        member = _fake_member(1, role_ids=[400])
        guild.members = [member]
        configs = AsyncMock()
        configs.get.return_value = _config(away_role_id=400)
        api = AsyncMock()
        api.get_away_now.return_value = []
        cog = _make_cog(api=api, configs=configs)

        await cog._sync_guild(guild)

        member.remove_roles.assert_awaited_once_with(role, reason="No longer marked away on sidequest-guild.com")
        member.add_roles.assert_not_awaited()

    async def test_no_discord_call_when_nothing_changes(self) -> None:
        role = _fake_role(400)
        guild = _fake_guild(roles=[role])
        member = _fake_member(1, role_ids=[400])
        guild.members = [member]
        configs = AsyncMock()
        configs.get.return_value = _config(away_role_id=400)
        api = AsyncMock()
        api.get_away_now.return_value = [1]
        cog = _make_cog(api=api, configs=configs)

        await cog._sync_guild(guild)

        member.add_roles.assert_not_awaited()
        member.remove_roles.assert_not_awaited()

    async def test_skips_bot_members(self) -> None:
        role = _fake_role(400)
        guild = _fake_guild(roles=[role])
        member = _fake_member(1, bot=True, role_ids=[])
        guild.members = [member]
        configs = AsyncMock()
        configs.get.return_value = _config(away_role_id=400)
        api = AsyncMock()
        api.get_away_now.return_value = [1]
        cog = _make_cog(api=api, configs=configs)

        await cog._sync_guild(guild)

        member.add_roles.assert_not_awaited()

    async def test_missing_role_skips_with_warning_only_once(self, caplog: pytest.LogCaptureFixture) -> None:
        guild = _fake_guild(roles=[])
        member = _fake_member(1, role_ids=[])
        guild.members = [member]
        configs = AsyncMock()
        configs.get.return_value = _config(away_role_id=400)
        api = AsyncMock()
        api.get_away_now.return_value = [1]
        cog = _make_cog(api=api, configs=configs)

        with caplog.at_level("WARNING"):
            await cog._sync_guild(guild)
            await cog._sync_guild(guild)

        member.add_roles.assert_not_awaited()
        assert sum("no longer exists" in message for message in caplog.messages) == 1


class TestSyncLoop:
    async def test_not_found_retries_next_tick_and_recovers(self) -> None:
        """A 404 from the new away-now endpoint (rolling deploy) must not permanently disable the cog."""
        guild = _fake_guild()
        configs = AsyncMock()
        configs.get.side_effect = [NotFoundError(404, "not deployed yet"), _config(away_role_id=None)]
        cog = _make_cog(configs=configs, guilds=[guild])

        await cog._sync_loop.coro(cog)
        await cog._sync_loop.coro(cog)

        assert configs.get.await_count == 2

    async def test_failed_fetch_does_not_crash_the_loop(self) -> None:
        guild = _fake_guild()
        configs = AsyncMock()
        configs.get.side_effect = APIError(500, "boom")
        cog = _make_cog(configs=configs, guilds=[guild])

        await cog._sync_loop.coro(cog)  # must not raise


class TestAwayGroup:
    async def test_role_blocks_a_role_the_invoker_may_not_configure(self) -> None:
        cog = _make_cog()
        interaction = _make_interaction()
        role = Mock(spec=discord.Role)
        with patch.object(away_module, "configure_block_reason", return_value="above your rank"):
            await AwayCog.away_role.callback(cog, interaction, role)
        interaction.response.send_message.assert_awaited_once_with("above your rank", ephemeral=True)
        interaction.client.configs.update.assert_not_awaited()

    async def test_role_sets_away_role_id_when_allowed(self) -> None:
        cog = _make_cog()
        interaction = _make_interaction()
        role = Mock(spec=discord.Role)
        role.id = 400
        role.mention = "<@&400>"
        with patch.object(away_module, "configure_block_reason", return_value=None):
            await AwayCog.away_role.callback(cog, interaction, role)
        update_call = interaction.client.configs.update.await_args
        assert update_call.args[0] == interaction.guild.id
        assert update_call.args[1].away_role_id == 400
        interaction.response.send_message.assert_awaited_once()

    async def test_role_clears_away_role_id_when_omitted(self) -> None:
        cog = _make_cog()
        interaction = _make_interaction()
        await AwayCog.away_role.callback(cog, interaction, None)
        update_call = interaction.client.configs.update.await_args
        assert update_call.args[1].away_role_id is None
