"""Unit tests for the event-sync plugin: event_state mapping, push/deletion application, loop and listener behavior."""

import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock
from uuid import UUID, uuid4

import discord
import pytest

from sidequest_bot.api_client import APIError, NotFoundError
from sidequest_bot.plugins.event_sync import EventSyncCog, event_state
from sidequest_common.schemas import DiscordEventPush, DiscordEventState, DiscordEventStatus


def _discord_error(cls: type[discord.HTTPException], *, status: int, reason: str = "error") -> discord.HTTPException:
    return cls(Mock(status=status, reason=reason), "boom")


def _fake_event(
    *,
    event_id: int = 1,
    guild_id: int = 42,
    name: str = "Raid Night",
    description: str | None = "desc",
    start_time: datetime = datetime(2026, 1, 1, tzinfo=UTC),
    end_time: datetime | None = datetime(2026, 1, 1, 3, tzinfo=UTC),
    status: discord.EventStatus = discord.EventStatus.scheduled,
    entity_type: discord.EntityType = discord.EntityType.external,
    location: str | None = "Molten Core",
    channel_id: int | None = None,
) -> MagicMock:
    event = MagicMock(spec=discord.ScheduledEvent)
    event.id = event_id
    event.guild_id = guild_id
    event.name = name
    event.description = description
    event.start_time = start_time
    event.end_time = end_time
    event.status = status
    event.entity_type = entity_type
    event.location = location
    event.channel_id = channel_id
    event.edit = AsyncMock(return_value=event)
    event.cancel = AsyncMock(return_value=event)
    event.end = AsyncMock(return_value=event)
    event.delete = AsyncMock(return_value=None)
    return event


def _fake_push(
    *,
    event_id: UUID | None = None,
    discord_event_id: int | None = None,
    action: str = "upsert",
    starts_at: datetime = datetime(2099, 1, 1, tzinfo=UTC),
    ends_at: datetime = datetime(2099, 1, 1, 3, tzinfo=UTC),
) -> DiscordEventPush:
    return DiscordEventPush(
        event_id=event_id or uuid4(),
        discord_event_id=discord_event_id,
        action=action,
        name="Raid Night",
        description="desc",
        location="Molten Core",
        starts_at=starts_at,
        ends_at=ends_at,
        push_hash="hash",
    )


def _fake_guild(*, guild_id: int = 42) -> MagicMock:
    guild = MagicMock(spec=discord.Guild)
    guild.id = guild_id
    guild.fetch_scheduled_event = AsyncMock()
    guild.fetch_scheduled_events = AsyncMock(return_value=[])
    guild.create_scheduled_event = AsyncMock()
    return guild


def _make_cog(*, api: object, guilds: list[object] | None = None) -> EventSyncCog:
    cog = EventSyncCog.__new__(EventSyncCog)
    cog.bot = SimpleNamespace(api=api, guilds=guilds or [])
    cog._reconciled_guild_ids = set()
    cog._not_site_guild_ids = set()
    cog._forbidden_guild_ids = set()
    return cog


class TestEventState:
    def test_maps_external_event_with_aware_utc(self) -> None:
        event = _fake_event()
        state = event_state(event)
        assert state == DiscordEventState(
            id=1,
            name="Raid Night",
            description="desc",
            starts_at=datetime(2026, 1, 1, tzinfo=UTC),
            ends_at=datetime(2026, 1, 1, 3, tzinfo=UTC),
            status=DiscordEventStatus.SCHEDULED,
            location="Molten Core",
            channel_id=None,
        )

    def test_naive_start_and_missing_end_are_handled(self) -> None:
        event = _fake_event(start_time=datetime(2026, 1, 1), end_time=None)
        state = event_state(event)
        assert state.starts_at == datetime(2026, 1, 1, tzinfo=UTC)
        assert state.ends_at is None

    def test_voice_event_reports_channel_not_location(self) -> None:
        event = _fake_event(entity_type=discord.EntityType.voice, channel_id=555, location=None)
        state = event_state(event)
        assert state.channel_id == 555
        assert state.location is None

    @pytest.mark.parametrize(
        ("discord_status", "wire_status"),
        [
            (discord.EventStatus.scheduled, DiscordEventStatus.SCHEDULED),
            (discord.EventStatus.active, DiscordEventStatus.ACTIVE),
            (discord.EventStatus.completed, DiscordEventStatus.COMPLETED),
            (discord.EventStatus.canceled, DiscordEventStatus.CANCELLED),
        ],
    )
    def test_status_mapping(self, discord_status: discord.EventStatus, wire_status: DiscordEventStatus) -> None:
        assert event_state(_fake_event(status=discord_status)).status == wire_status


class TestApplyUpsert:
    async def test_creates_external_event_when_unlinked(self) -> None:
        guild = _fake_guild()
        created = _fake_event(event_id=99)
        guild.create_scheduled_event.return_value = created
        cog = _make_cog(api=AsyncMock())

        state = await cog._apply_upsert(guild, _fake_push(discord_event_id=None))

        guild.create_scheduled_event.assert_awaited_once()
        kwargs = guild.create_scheduled_event.await_args.kwargs
        assert kwargs["entity_type"] is discord.EntityType.external
        assert kwargs["privacy_level"] is discord.PrivacyLevel.guild_only
        assert state == event_state(created)

    async def test_past_start_with_no_id_skips_creation(self) -> None:
        guild = _fake_guild()
        cog = _make_cog(api=AsyncMock())
        push = _fake_push(
            discord_event_id=None,
            starts_at=datetime(2000, 1, 1, tzinfo=UTC),
            ends_at=datetime(2000, 1, 1, 3, tzinfo=UTC),
        )

        state = await cog._apply_upsert(guild, push)

        guild.create_scheduled_event.assert_not_awaited()
        assert state is None

    async def test_scheduled_event_edit_includes_start(self) -> None:
        existing = _fake_event(status=discord.EventStatus.scheduled)
        guild = _fake_guild()
        guild.fetch_scheduled_event.return_value = existing
        cog = _make_cog(api=AsyncMock())

        state = await cog._apply_upsert(guild, _fake_push(discord_event_id=existing.id))

        existing.edit.assert_awaited_once()
        assert "start_time" in existing.edit.await_args.kwargs
        assert state == event_state(existing)

    async def test_active_event_edit_omits_start(self) -> None:
        existing = _fake_event(status=discord.EventStatus.active)
        guild = _fake_guild()
        guild.fetch_scheduled_event.return_value = existing
        cog = _make_cog(api=AsyncMock())

        await cog._apply_upsert(guild, _fake_push(discord_event_id=existing.id))

        existing.edit.assert_awaited_once()
        assert "start_time" not in existing.edit.await_args.kwargs

    @pytest.mark.parametrize("status", [discord.EventStatus.completed, discord.EventStatus.canceled])
    async def test_ended_event_acks_none(self, status: discord.EventStatus) -> None:
        existing = _fake_event(status=status)
        guild = _fake_guild()
        guild.fetch_scheduled_event.return_value = existing
        cog = _make_cog(api=AsyncMock())

        state = await cog._apply_upsert(guild, _fake_push(discord_event_id=existing.id))

        existing.edit.assert_not_awaited()
        assert state is None


class TestApplyCancelOrComplete:
    async def test_cancel_scheduled_event(self) -> None:
        existing = _fake_event(status=discord.EventStatus.scheduled)
        guild = _fake_guild()
        guild.fetch_scheduled_event.return_value = existing
        cog = _make_cog(api=AsyncMock())

        state = await cog._apply_cancel_or_complete(guild, _fake_push(action="cancel", discord_event_id=existing.id))

        existing.cancel.assert_awaited_once()
        existing.end.assert_not_awaited()
        assert state is None

    async def test_cancel_active_event_ends_it(self) -> None:
        existing = _fake_event(status=discord.EventStatus.active)
        guild = _fake_guild()
        guild.fetch_scheduled_event.return_value = existing
        cog = _make_cog(api=AsyncMock())

        await cog._apply_cancel_or_complete(guild, _fake_push(action="cancel", discord_event_id=existing.id))

        existing.end.assert_awaited_once()
        existing.cancel.assert_not_awaited()

    async def test_complete_active_event_ends_it(self) -> None:
        existing = _fake_event(status=discord.EventStatus.active)
        guild = _fake_guild()
        guild.fetch_scheduled_event.return_value = existing
        cog = _make_cog(api=AsyncMock())

        await cog._apply_cancel_or_complete(guild, _fake_push(action="complete", discord_event_id=existing.id))

        existing.end.assert_awaited_once()
        existing.delete.assert_not_awaited()

    async def test_complete_scheduled_event_deletes_it(self) -> None:
        existing = _fake_event(status=discord.EventStatus.scheduled)
        guild = _fake_guild()
        guild.fetch_scheduled_event.return_value = existing
        cog = _make_cog(api=AsyncMock())

        await cog._apply_cancel_or_complete(guild, _fake_push(action="complete", discord_event_id=existing.id))

        existing.delete.assert_awaited_once()
        existing.end.assert_not_awaited()

    async def test_missing_event_does_nothing(self) -> None:
        guild = _fake_guild()
        guild.fetch_scheduled_event.side_effect = _discord_error(discord.NotFound, status=404)
        cog = _make_cog(api=AsyncMock())

        state = await cog._apply_cancel_or_complete(guild, _fake_push(action="cancel", discord_event_id=123))

        assert state is None


class TestApplyDeletion:
    async def test_deletes_event_then_clears_tombstone(self) -> None:
        existing = _fake_event(event_id=7)
        guild = _fake_guild()
        guild.fetch_scheduled_event.return_value = existing
        api = AsyncMock()
        cog = _make_cog(api=api)

        await cog._apply_deletion(guild, 7)

        existing.delete.assert_awaited_once()
        api.delete_event_sync_tombstone.assert_awaited_once_with(guild.id, 7)

    async def test_discord_404_still_clears_tombstone(self) -> None:
        guild = _fake_guild()
        guild.fetch_scheduled_event.side_effect = _discord_error(discord.NotFound, status=404)
        api = AsyncMock()
        cog = _make_cog(api=api)

        await cog._apply_deletion(guild, 7)

        api.delete_event_sync_tombstone.assert_awaited_once_with(guild.id, 7)


class TestApplyOneAndPush:
    async def test_forbidden_warns_once_and_skips_the_cycle(self, caplog: pytest.LogCaptureFixture) -> None:
        cog = _make_cog(api=AsyncMock())
        guild = _fake_guild()
        forbidden = _discord_error(discord.Forbidden, status=403)

        async def _raise() -> None:
            raise forbidden

        with caplog.at_level(logging.WARNING):
            result1 = await cog._apply_one(guild, _raise())
            result2 = await cog._apply_one(guild, _raise())

        assert result1 is False
        assert result2 is False
        warnings = [r for r in caplog.records if "Missing Manage Events permission" in r.message]
        assert len(warnings) == 1

    async def test_forbidden_during_push_skips_ack(self) -> None:
        guild = _fake_guild()
        guild.create_scheduled_event.side_effect = _discord_error(discord.Forbidden, status=403)
        api = AsyncMock()
        cog = _make_cog(api=api)

        with pytest.raises(discord.Forbidden):
            await cog._apply_push(guild, _fake_push(discord_event_id=None))

        api.ack_event_push.assert_not_awaited()

    async def test_failing_push_does_not_stop_later_pushes(self) -> None:
        cog = _make_cog(api=AsyncMock())
        guild = _fake_guild()

        async def _fail() -> None:
            raise _discord_error(discord.HTTPException, status=500)

        async def _ok() -> None:
            return None

        result1 = await cog._apply_one(guild, _fail())
        result2 = await cog._apply_one(guild, _ok())

        assert result1 is True
        assert result2 is True

    async def test_success_resets_the_forbidden_flag(self) -> None:
        cog = _make_cog(api=AsyncMock())
        guild = _fake_guild()
        cog._forbidden_guild_ids.add(guild.id)

        async def _ok() -> None:
            return None

        await cog._apply_one(guild, _ok())

        assert guild.id not in cog._forbidden_guild_ids


class TestSyncLoop:
    async def test_not_found_for_guild_stops_syncing_it(self) -> None:
        guild = _fake_guild()
        api = AsyncMock()
        api.reconcile_event_sync.side_effect = NotFoundError(404, "not the site guild")
        cog = _make_cog(api=api, guilds=[guild])

        await cog._sync_loop.coro(cog)
        await cog._sync_loop.coro(cog)

        assert guild.id in cog._not_site_guild_ids
        assert api.reconcile_event_sync.await_count == 1

    async def test_other_api_errors_do_not_crash_the_loop(self) -> None:
        guild = _fake_guild()
        api = AsyncMock()
        api.reconcile_event_sync.side_effect = APIError(500, "boom")
        cog = _make_cog(api=api, guilds=[guild])

        await cog._sync_loop.coro(cog)  # must not raise

        assert guild.id not in cog._not_site_guild_ids


class TestListeners:
    async def test_create_reports_state(self) -> None:
        event = _fake_event()
        api = AsyncMock()
        cog = _make_cog(api=api)

        await cog.on_scheduled_event_create(event)

        api.report_discord_event.assert_awaited_once_with(event.guild_id, event_state(event))

    async def test_update_reports_after_state(self) -> None:
        before = _fake_event(name="Old")
        after = _fake_event(name="New")
        api = AsyncMock()
        cog = _make_cog(api=api)

        await cog.on_scheduled_event_update(before, after)

        api.report_discord_event.assert_awaited_once_with(after.guild_id, event_state(after))

    async def test_delete_reports_deletion(self) -> None:
        event = _fake_event(event_id=5)
        api = AsyncMock()
        cog = _make_cog(api=api)

        await cog.on_scheduled_event_delete(event)

        api.report_discord_event_deleted.assert_awaited_once_with(event.guild_id, 5)

    async def test_not_found_marks_guild_not_site(self) -> None:
        event = _fake_event()
        api = AsyncMock()
        api.report_discord_event.side_effect = NotFoundError(404, "nope")
        cog = _make_cog(api=api)

        await cog.on_scheduled_event_create(event)

        assert event.guild_id in cog._not_site_guild_ids

    async def test_skips_known_non_site_guild(self) -> None:
        event = _fake_event()
        api = AsyncMock()
        cog = _make_cog(api=api)
        cog._not_site_guild_ids.add(event.guild_id)

        await cog.on_scheduled_event_create(event)

        api.report_discord_event.assert_not_awaited()
