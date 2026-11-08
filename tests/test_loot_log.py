"""Unit tests for the loot_log plugin: embeds, the post loop, and ``/lootlog channel``."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock
from uuid import uuid4

import discord

from sidequest_bot.api_client import APIError, NotFoundError
from sidequest_bot.plugins.loot_log import (
    LootLogCog,
    build_decision_embed,
    build_entry_embed,
    build_void_embed,
    quality_color,
)
from sidequest_common.schemas import CouncilKind, CouncilLogEntry, CouncilLogKind, GuildConfigUpdate


def _entry(**overrides: object) -> CouncilLogEntry:
    base: dict[str, object] = {
        "loot_id": uuid4(),
        "kind": CouncilLogKind.DECISION,
        "item_id": 19019,
        "item_name": "Thunderfury, Blessed Blade of the Windseeker",
        "quality": 4,
        "character": "Arthas",
        "member_name": "Arthas",
        "gp": 50,
        "council_kind": CouncilKind.CHASE,
        "council_reason": "Best in slot for this tier",
        "instance_name": "Molten Core",
        "encounter_name": "Ragnaros",
        "at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    base.update(overrides)
    return CouncilLogEntry(**base)


def _config(*, loot_log_channel_id: int | None = None) -> SimpleNamespace:
    return SimpleNamespace(loot_log_channel_id=loot_log_channel_id)


def _fake_guild(guild_id: int = 42) -> MagicMock:
    guild = MagicMock(spec=discord.Guild)
    guild.id = guild_id
    guild.get_channel.return_value = None
    return guild


def _fake_channel(channel_id: int = 900, *, guild_id: int = 42) -> MagicMock:
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = channel_id
    channel.send = AsyncMock()
    channel.guild = MagicMock(spec=discord.Guild, id=guild_id)
    return channel


def _make_cog(
    *, api: object | None = None, configs: object | None = None, guilds: list[object] | None = None
) -> LootLogCog:
    cog = LootLogCog.__new__(LootLogCog)
    cog.bot = SimpleNamespace(api=api or AsyncMock(), configs=configs or AsyncMock(), guilds=guilds or [])
    return cog


class TestQualityColor:
    def test_known_quality_returns_wow_color(self) -> None:
        assert quality_color(4) == discord.Colour(0xA335EE)

    def test_unknown_quality_returns_house_slate(self) -> None:
        from sidequest_bot.lib.embeds import Colors

        assert quality_color(99) == Colors.SLATE


class TestBuildDecisionEmbed:
    def test_includes_winner_gp_kind_reason_and_raid(self) -> None:
        embed = build_decision_embed(_entry())

        assert embed.title == "Thunderfury, Blessed Blade of the Windseeker"
        assert embed.url == "https://www.wowhead.com/forever/item=19019"
        field_values = {field.name: field.value for field in embed.fields}
        assert field_values["Winner"] == "Arthas"
        assert field_values["GP"] == "50"
        assert field_values["Kind"] == "Chase item"
        assert field_values["Reason"] == "Best in slot for this tier"
        assert field_values["Raid"] == "Molten Core — Ragnaros"

    def test_falls_back_to_character_when_member_name_unknown(self) -> None:
        embed = build_decision_embed(_entry(member_name=None, character="Arthas"))

        field_values = {field.name: field.value for field in embed.fields}
        assert field_values["Winner"] == "Arthas"

    def test_omits_raid_field_when_instance_unknown(self) -> None:
        embed = build_decision_embed(_entry(instance_name=None, encounter_name=None))

        assert "Raid" not in {field.name for field in embed.fields}

    def test_omits_kind_and_reason_fields_when_unset(self) -> None:
        embed = build_decision_embed(_entry(council_kind=None, council_reason=None))

        field_names = {field.name for field in embed.fields}
        assert "Kind" not in field_names
        assert "Reason" not in field_names

    def test_legendary_and_tier_kind_labels(self) -> None:
        legendary = build_decision_embed(_entry(council_kind=CouncilKind.LEGENDARY))
        tier = build_decision_embed(_entry(council_kind=CouncilKind.TIER))

        legendary_fields = {field.name: field.value for field in legendary.fields}
        tier_fields = {field.name: field.value for field in tier.fields}
        assert legendary_fields["Kind"] == "Legendary or special item"
        assert tier_fields["Kind"] == "Crafted tier"


class TestBuildVoidEmbed:
    def test_short_voided_note(self) -> None:
        embed = build_void_embed(_entry(kind=CouncilLogKind.VOID, item_name="Thunderfury", member_name="Arthas"))

        assert embed.description == "Council award voided: Thunderfury to Arthas"
        assert not embed.fields


class TestBuildEntryEmbed:
    def test_decision_kind_builds_decision_embed(self) -> None:
        embed = build_entry_embed(_entry(kind=CouncilLogKind.DECISION))
        assert embed.title is not None

    def test_void_kind_builds_void_embed(self) -> None:
        embed = build_entry_embed(_entry(kind=CouncilLogKind.VOID))
        assert embed.title is None
        assert embed.description is not None


class TestSyncGuild:
    async def test_no_channel_set_does_not_fetch_pending(self) -> None:
        guild = _fake_guild()
        api = AsyncMock()
        configs = AsyncMock()
        configs.get.return_value = _config(loot_log_channel_id=None)
        cog = _make_cog(api=api, configs=configs)

        await cog._sync_guild(guild)

        api.pending_council_log.assert_not_awaited()
        api.ack_council_log.assert_not_awaited()

    async def test_channel_id_not_resolving_to_text_channel_does_not_fetch_pending(self) -> None:
        guild = _fake_guild()
        guild.get_channel.return_value = None
        api = AsyncMock()
        configs = AsyncMock()
        configs.get.return_value = _config(loot_log_channel_id=900)
        cog = _make_cog(api=api, configs=configs)

        await cog._sync_guild(guild)

        api.pending_council_log.assert_not_awaited()

    async def test_pending_entries_are_posted_and_acked(self) -> None:
        channel = _fake_channel()
        guild = _fake_guild()
        guild.get_channel.return_value = channel
        api = AsyncMock()
        configs = AsyncMock()
        configs.get.return_value = _config(loot_log_channel_id=900)
        entry_one, entry_two = _entry(), _entry(kind=CouncilLogKind.VOID)
        api.pending_council_log.return_value = [entry_one, entry_two]
        cog = _make_cog(api=api, configs=configs)

        await cog._sync_guild(guild)

        assert channel.send.await_count == 2
        assert api.ack_council_log.await_count == 2
        api.ack_council_log.assert_any_await(42, entry_one.loot_id, CouncilLogKind.DECISION)
        api.ack_council_log.assert_any_await(42, entry_two.loot_id, CouncilLogKind.VOID)

    async def test_forbidden_leaves_entries_pending_without_acking(self) -> None:
        channel = _fake_channel()
        channel.send.side_effect = discord.Forbidden(MagicMock(), "missing permission")
        guild = _fake_guild()
        guild.get_channel.return_value = channel
        api = AsyncMock()
        configs = AsyncMock()
        configs.get.return_value = _config(loot_log_channel_id=900)
        api.pending_council_log.return_value = [_entry(), _entry()]
        cog = _make_cog(api=api, configs=configs)

        await cog._sync_guild(guild)  # must not raise

        api.ack_council_log.assert_not_awaited()
        assert channel.send.await_count == 1  # stops after the first failure instead of retrying every entry

    async def test_not_found_leaves_entries_pending_without_acking(self) -> None:
        channel = _fake_channel()
        channel.send.side_effect = discord.NotFound(MagicMock(), "unknown channel")
        guild = _fake_guild()
        guild.get_channel.return_value = channel
        api = AsyncMock()
        configs = AsyncMock()
        configs.get.return_value = _config(loot_log_channel_id=900)
        api.pending_council_log.return_value = [_entry()]
        cog = _make_cog(api=api, configs=configs)

        await cog._sync_guild(guild)  # must not raise

        api.ack_council_log.assert_not_awaited()

    async def test_ack_failure_does_not_raise(self) -> None:
        channel = _fake_channel()
        guild = _fake_guild()
        guild.get_channel.return_value = channel
        api = AsyncMock()
        configs = AsyncMock()
        configs.get.return_value = _config(loot_log_channel_id=900)
        api.pending_council_log.return_value = [_entry()]
        api.ack_council_log.side_effect = APIError(500, "boom")
        cog = _make_cog(api=api, configs=configs)

        await cog._sync_guild(guild)  # must not raise

        channel.send.assert_awaited_once()


class TestSyncLoop:
    async def test_not_found_retries_next_tick(self) -> None:
        guild = _fake_guild()
        api = AsyncMock()
        configs = AsyncMock()
        configs.get.side_effect = NotFoundError(404, "not deployed yet")
        cog = _make_cog(api=api, configs=configs, guilds=[guild])

        await cog._sync_loop.coro(cog)  # must not raise

    async def test_api_error_does_not_stop_the_loop(self) -> None:
        guild = _fake_guild()
        api = AsyncMock()
        configs = AsyncMock()
        configs.get.side_effect = APIError(500, "boom")
        cog = _make_cog(api=api, configs=configs, guilds=[guild])

        await cog._sync_loop.coro(cog)  # must not raise


class TestLootlogChannelCommand:
    async def test_sets_channel(self) -> None:
        guild = Mock(spec=discord.Guild)
        guild.id = 42
        channel = Mock(spec=discord.TextChannel)
        channel.id = 900
        channel.mention = "#loot-log"
        interaction = Mock(spec=discord.Interaction)
        interaction.guild = guild
        interaction.response = Mock()
        interaction.response.send_message = AsyncMock()
        interaction.client = Mock()
        interaction.client.configs = Mock()
        interaction.client.configs.update = AsyncMock()
        cog = _make_cog()

        await cog.lootlog_channel.callback(cog, interaction, channel)

        update_call = interaction.client.configs.update.await_args
        assert update_call.args[0] == 42
        assert update_call.args[1] == GuildConfigUpdate(loot_log_channel_id=900)
        interaction.response.send_message.assert_awaited_once()

    async def test_clears_channel(self) -> None:
        guild = Mock(spec=discord.Guild)
        guild.id = 42
        interaction = Mock(spec=discord.Interaction)
        interaction.guild = guild
        interaction.response = Mock()
        interaction.response.send_message = AsyncMock()
        interaction.client = Mock()
        interaction.client.configs = Mock()
        interaction.client.configs.update = AsyncMock()
        cog = _make_cog()

        await cog.lootlog_channel.callback(cog, interaction, None)

        update_call = interaction.client.configs.update.await_args
        assert update_call.args[1] == GuildConfigUpdate(loot_log_channel_id=None)
