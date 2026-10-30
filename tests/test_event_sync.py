"""Unit tests for the event-sync plugin: event_state mapping, push/deletion application, loop and listener behavior."""

import asyncio
import contextlib
import logging
from datetime import UTC, datetime
from http import HTTPStatus
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock
from uuid import UUID, uuid4

import discord
import pytest

from sidequest_bot.api_client import APIError, NotFoundError
from sidequest_bot.plugins.event_sync import (
    ChangeSignupDetailsButton,
    EventSignupButton,
    EventSyncCog,
    SignupDetailsModal,
    _confirmation_text,
    _EnterDetailsView,
    _find_tag,
    _guess_class_from_roles,
    _handle_signup_click,
    _send_sign_in_prompt,
    _submit_signup,
    build_activity_line,
    build_change_custom_id,
    build_counts_line,
    build_signup_custom_id,
    build_signup_view,
    build_starter_message,
    event_state,
    parse_change_custom_id,
    parse_signup_custom_id,
)
from sidequest_common.schemas import (
    DiscordEventPush,
    DiscordEventState,
    DiscordEventStatus,
    EventCounts,
    EventKind,
    Signup,
    SignupActivity,
    SignupByDiscord,
    SignupDefaults,
    SignupMember,
    SignupRole,
    SignupStatus,
    WowClass,
)


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
    thread_id: int | None = None,
    kind: EventKind = EventKind.RAID,
    action: str = "upsert",
    starts_at: datetime = datetime(2099, 1, 1, tzinfo=UTC),
    ends_at: datetime = datetime(2099, 1, 1, 3, tzinfo=UTC),
    counts: EventCounts | None = None,
) -> DiscordEventPush:
    return DiscordEventPush(
        event_id=event_id or uuid4(),
        discord_event_id=discord_event_id,
        thread_id=thread_id,
        kind=kind,
        counts=counts or EventCounts(tank=0, healer=0, dps=0, signed=0, tentative=0, absent=0, interested=0),
        action=action,
        name="Raid Night",
        title="Raid Night",
        description="desc",
        location="Molten Core",
        starts_at=starts_at,
        ends_at=ends_at,
        signup_url="https://example.com/events/detail/?id=abc",
        push_hash="hash",
    )


def _fake_guild(*, guild_id: int = 42) -> MagicMock:
    guild = MagicMock(spec=discord.Guild)
    guild.id = guild_id
    guild.fetch_scheduled_event = AsyncMock()
    guild.fetch_scheduled_events = AsyncMock(return_value=[])
    guild.create_scheduled_event = AsyncMock()
    return guild


def _make_cog(*, api: object, guilds: list[object] | None = None, forum_channel_id: int | None = None) -> EventSyncCog:
    cog = EventSyncCog.__new__(EventSyncCog)
    configs = AsyncMock()
    configs.get.return_value = SimpleNamespace(events_forum_channel_id=forum_channel_id)
    cog.bot = SimpleNamespace(api=api, guilds=guilds or [], configs=configs)
    cog._reconciled_guild_ids = set()
    cog._not_site_guild_ids = set()
    cog._forbidden_guild_ids = set()
    cog._forum_forbidden_guild_ids = set()
    cog._tag_forbidden_guild_ids = set()
    cog._thread_ids = {}
    cog._interest_tasks = {}
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


def _fake_forum(*, guild_id: int = 42, available_tags: list | None = None) -> MagicMock:
    guild = MagicMock(spec=discord.Guild)
    guild.id = guild_id
    guild.get_thread = MagicMock(return_value=None)
    guild.fetch_channel = AsyncMock()
    forum = MagicMock(spec=discord.ForumChannel)
    forum.guild = guild
    forum.available_tags = available_tags or []
    forum.create_tag = AsyncMock()
    forum.create_thread = AsyncMock()
    return forum


class TestBuildStarterMessage:
    def test_fits_within_2000_chars_with_long_notes(self) -> None:
        push = _fake_push(discord_event_id=1).model_copy(update={"description": "x" * 5000})
        message = build_starter_message(push, guild_id=42, discord_event_id=1)
        assert len(message) <= 2000

    def test_includes_header_time_notes_and_links(self) -> None:
        push = _fake_push(discord_event_id=1, kind=EventKind.SOCIAL).model_copy(
            update={"location": "Stormwind", "description": "Come hang out"}
        )
        message = build_starter_message(push, guild_id=42, discord_event_id=1)
        assert "Social · Stormwind" in message
        assert "Come hang out" in message
        assert "Sign up: " in message
        assert "https://discord.com/events/42/1" in message


class TestBuildActivityLine:
    @pytest.mark.parametrize(
        ("action", "expected"),
        [
            ("going", "✅ **Coffee** is going"),
            ("maybe", "❔ **Coffee** might make it"),
            ("not_going", "❌ **Coffee** can't make it"),
            ("withdrew", "↩️ **Coffee** withdrew"),
        ],
    )
    def test_formats_each_action_without_detail(self, action: str, expected: str) -> None:
        activity = SignupActivity(id=uuid4(), thread_id=1, display_name="Coffee", action=action, detail=None)
        assert build_activity_line(activity) == expected

    def test_appends_detail_when_present(self) -> None:
        activity = SignupActivity(
            id=uuid4(), thread_id=1, display_name="Coffee", action="going", detail="Oakenshield · Warrior · Tank"
        )
        assert build_activity_line(activity) == "✅ **Coffee** is going — Oakenshield · Warrior · Tank"

    def test_mentions_member_and_quotes_comment_escaped(self) -> None:
        activity = SignupActivity(
            id=uuid4(),
            thread_id=1,
            display_name="Bad Messages",
            action="going",
            discord_id=229053264241360900,
            comment="  Can we play *Hello Kitty* instead?\n# please\n- [free](https://x.test) - ok  ",
        )
        assert build_activity_line(activity) == (
            "✅ <@229053264241360900> is going\n"
            "> Can we play \\*Hello Kitty\\* instead?\n"
            "> \\# please\n"
            "> \\- \\[free\\](https://x.test) - ok"
        )

    def test_blank_comment_adds_nothing(self) -> None:
        activity = SignupActivity(
            id=uuid4(), thread_id=1, display_name="Coffee", action="maybe", discord_id=5, comment="   "
        )
        assert build_activity_line(activity) == "❔ <@5> might make it"


class TestFindTag:
    def test_matches_by_kind_label(self) -> None:
        raid_tag = SimpleNamespace(name="Raid")
        social_tag = SimpleNamespace(name="Social")
        assert _find_tag([raid_tag, social_tag], EventKind.SOCIAL) is social_tag

    def test_returns_none_when_missing(self) -> None:
        assert _find_tag([], EventKind.RAID) is None


class TestEnsureForumPost:
    async def test_no_forum_configured_returns_existing_thread_id(self) -> None:
        guild = _fake_guild()
        cog = _make_cog(api=AsyncMock())
        push = _fake_push(thread_id=42)
        result = await cog._ensure_forum_post(guild, push, event_state(_fake_event()), 1)
        assert result == 42

    async def test_wrong_channel_type_returns_existing_thread_id(self) -> None:
        guild = _fake_guild()
        guild.get_channel = MagicMock(return_value=MagicMock(spec=discord.TextChannel))
        cog = _make_cog(api=AsyncMock(), forum_channel_id=99)
        push = _fake_push(thread_id=42)
        result = await cog._ensure_forum_post(guild, push, event_state(_fake_event()), 1)
        assert result == 42


class TestEnsureTag:
    async def test_returns_existing_tag_without_creating(self) -> None:
        existing = MagicMock(spec=discord.ForumTag)
        existing.name = "Raid"
        forum = _fake_forum(available_tags=[existing])
        cog = _make_cog(api=AsyncMock())

        tag = await cog._ensure_tag(forum, EventKind.RAID)

        assert tag is existing
        forum.create_tag.assert_not_awaited()

    async def test_creates_missing_tag(self) -> None:
        forum = _fake_forum()
        created = MagicMock(spec=discord.ForumTag)
        forum.create_tag.return_value = created
        cog = _make_cog(api=AsyncMock())

        tag = await cog._ensure_tag(forum, EventKind.DUNGEON)

        assert tag is created
        forum.create_tag.assert_awaited_once_with(name="Dungeon")

    async def test_forbidden_warns_once_and_returns_none(self, caplog: pytest.LogCaptureFixture) -> None:
        forum = _fake_forum()
        forum.create_tag.side_effect = _discord_error(discord.Forbidden, status=403)
        cog = _make_cog(api=AsyncMock())

        with caplog.at_level(logging.WARNING):
            first = await cog._ensure_tag(forum, EventKind.RAID)
            second = await cog._ensure_tag(forum, EventKind.RAID)

        assert first is None
        assert second is None
        warnings = [r for r in caplog.records if "Manage Channels" in r.message]
        assert len(warnings) == 1


class TestEnsureUpsertThread:
    async def test_creates_thread_with_tag_when_none_exists(self) -> None:
        forum = _fake_forum()
        tag = MagicMock(spec=discord.ForumTag)
        forum.create_tag.return_value = tag
        created_thread = MagicMock()
        created_thread.id = 999
        forum.create_thread.return_value = SimpleNamespace(thread=created_thread, message=MagicMock())
        cog = _make_cog(api=AsyncMock())
        push = _fake_push(discord_event_id=555, kind=EventKind.RAID)

        thread_id = await cog._ensure_upsert_thread(forum, push, 555)

        assert thread_id == 999
        kwargs = forum.create_thread.await_args.kwargs
        assert kwargs["applied_tags"] == [tag]
        assert kwargs["name"] == push.title[:100]

    async def test_edits_existing_thread_name_and_starter(self) -> None:
        forum = _fake_forum()
        thread = MagicMock(spec=discord.Thread)
        thread.id = 42
        thread.edit = AsyncMock()
        starter = MagicMock()
        starter.edit = AsyncMock()
        thread.fetch_message = AsyncMock(return_value=starter)
        forum.guild.get_thread.return_value = thread
        cog = _make_cog(api=AsyncMock())
        push = _fake_push(discord_event_id=555, thread_id=42)

        thread_id = await cog._ensure_upsert_thread(forum, push, 555)

        assert thread_id == 42
        thread.edit.assert_awaited_once_with(name=push.title[:100])
        starter.edit.assert_awaited_once()

    async def test_missing_thread_returns_none(self) -> None:
        forum = _fake_forum()
        forum.guild.fetch_channel.side_effect = _discord_error(discord.NotFound, status=404)
        cog = _make_cog(api=AsyncMock())
        push = _fake_push(discord_event_id=555, thread_id=42)

        thread_id = await cog._ensure_upsert_thread(forum, push, 555)

        assert thread_id is None


class TestEnsureCancelOrCompleteThread:
    async def test_cancel_posts_message_and_archives_locks(self) -> None:
        forum = _fake_forum()
        thread = MagicMock(spec=discord.Thread)
        thread.send = AsyncMock()
        thread.edit = AsyncMock()
        forum.guild.get_thread.return_value = thread
        cog = _make_cog(api=AsyncMock())
        push = _fake_push(action="cancel", thread_id=42)

        thread_id = await cog._ensure_cancel_or_complete_thread(forum, push)

        assert thread_id == 42
        thread.send.assert_awaited_once()
        thread.edit.assert_awaited_once_with(archived=True, locked=True)

    async def test_complete_archives_without_message(self) -> None:
        forum = _fake_forum()
        thread = MagicMock(spec=discord.Thread)
        thread.send = AsyncMock()
        thread.edit = AsyncMock()
        forum.guild.get_thread.return_value = thread
        cog = _make_cog(api=AsyncMock())
        push = _fake_push(action="complete", thread_id=42)

        await cog._ensure_cancel_or_complete_thread(forum, push)

        thread.send.assert_not_awaited()
        thread.edit.assert_awaited_once_with(archived=True, locked=True)

    async def test_no_known_thread_returns_none(self) -> None:
        forum = _fake_forum()
        cog = _make_cog(api=AsyncMock())
        push = _fake_push(action="cancel", thread_id=None)

        assert await cog._ensure_cancel_or_complete_thread(forum, push) is None


class TestUpgradeExistingForumThreads:
    """Finding 1: an old worker can ack a starter edit without buttons, so the hash-driven push never retries it."""

    async def test_adds_the_view_to_a_thread_missing_sign_up_buttons(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(asyncio, "sleep", AsyncMock())
        forum = _fake_forum()
        event_id = uuid4()
        thread = MagicMock(spec=discord.Thread)
        thread.id = 42
        starter = MagicMock(
            content=f"Sign up: https://sidequest-guild.com/events/detail/?id={event_id}\n", components=[]
        )
        starter.edit = AsyncMock()
        thread.fetch_message = AsyncMock(return_value=starter)
        forum.threads = [thread]
        cog = _make_cog(api=AsyncMock(), guilds=[forum.guild], forum_channel_id=900)
        forum.guild.get_channel.return_value = forum

        await cog._upgrade_existing_forum_threads(forum.guild)

        starter.edit.assert_awaited_once()
        view = starter.edit.await_args.kwargs["view"]
        assert any(getattr(item, "custom_id", "").startswith(f"sq:ev:{event_id}:") for item in view.children)

    async def test_leaves_a_thread_that_already_has_buttons_alone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(asyncio, "sleep", AsyncMock())
        forum = _fake_forum()
        thread = MagicMock(spec=discord.Thread)
        thread.id = 42
        starter = MagicMock(
            content="Sign up: https://sidequest-guild.com/events/detail/?id=not-checked", components=[MagicMock()]
        )
        starter.edit = AsyncMock()
        thread.fetch_message = AsyncMock(return_value=starter)
        forum.threads = [thread]
        cog = _make_cog(api=AsyncMock(), guilds=[forum.guild], forum_channel_id=900)
        forum.guild.get_channel.return_value = forum

        await cog._upgrade_existing_forum_threads(forum.guild)

        starter.edit.assert_not_awaited()

    async def test_no_forum_configured_does_nothing(self) -> None:
        guild = _fake_guild()
        cog = _make_cog(api=AsyncMock(), guilds=[guild], forum_channel_id=None)

        await cog._upgrade_existing_forum_threads(guild)  # must not raise; no get_channel call needed


class TestCloseForumThreadForDeletion:
    async def test_posts_removal_notice_and_archives(self) -> None:
        guild = _fake_guild()
        thread = MagicMock(spec=discord.Thread)
        thread.send = AsyncMock()
        thread.edit = AsyncMock()
        guild.get_thread = MagicMock(return_value=thread)
        cog = _make_cog(api=AsyncMock())
        cog._thread_ids[555] = 42

        await cog._close_forum_thread_for_deletion(guild, 555)

        thread.send.assert_awaited_once()
        thread.edit.assert_awaited_once_with(archived=True, locked=True)
        assert 555 not in cog._thread_ids

    async def test_noop_when_thread_unknown(self) -> None:
        guild = _fake_guild()
        cog = _make_cog(api=AsyncMock())

        await cog._close_forum_thread_for_deletion(guild, 999)  # must not raise


class TestPostActivity:
    async def test_posts_each_row_and_acks_all(self) -> None:
        guild = _fake_guild()
        thread = MagicMock(spec=discord.Thread)
        thread.send = AsyncMock()
        guild.get_thread = MagicMock(return_value=thread)
        api = AsyncMock()
        cog = _make_cog(api=api)
        rows = [
            SignupActivity(id=uuid4(), thread_id=1, display_name="Coffee", action="going", detail=None),
            SignupActivity(id=uuid4(), thread_id=1, display_name="Wren", action="maybe", detail=None),
        ]

        await cog._post_activity(guild, rows)

        assert thread.send.await_count == 2
        acked_ids = api.ack_event_activity.await_args.args[1]
        assert set(acked_ids) == {row.id for row in rows}

    async def test_missing_thread_still_acks(self) -> None:
        guild = _fake_guild()
        guild.get_thread = MagicMock(return_value=None)
        guild.fetch_channel.side_effect = _discord_error(discord.NotFound, status=404)
        api = AsyncMock()
        cog = _make_cog(api=api)
        row = SignupActivity(id=uuid4(), thread_id=1, display_name="Kay", action="withdrew", detail=None)

        await cog._post_activity(guild, [row])

        api.ack_event_activity.assert_awaited_once_with(guild.id, [row.id])

    async def test_forbidden_on_send_skips_ack_for_that_row(self) -> None:
        guild = _fake_guild()
        thread = MagicMock(spec=discord.Thread)
        thread.send = AsyncMock(side_effect=_discord_error(discord.Forbidden, status=403))
        guild.get_thread = MagicMock(return_value=thread)
        api = AsyncMock()
        cog = _make_cog(api=api)
        row = SignupActivity(id=uuid4(), thread_id=1, display_name="Kay", action="going", detail=None)

        await cog._post_activity(guild, [row])

        api.ack_event_activity.assert_not_awaited()


class TestReportInterest:
    async def test_reports_user_ids_excluding_bots(self) -> None:
        users = [SimpleNamespace(id=1, bot=False), SimpleNamespace(id=2, bot=True), SimpleNamespace(id=3, bot=False)]

        async def _users_iter() -> object:
            for user in users:
                yield user

        event = SimpleNamespace(id=10, guild_id=42, users=_users_iter)
        api = AsyncMock()
        cog = _make_cog(api=api)

        await cog._report_interest(event)

        api.report_event_interest.assert_awaited_once()
        call_args = api.report_event_interest.await_args.args
        assert call_args[0] == 42
        assert call_args[1] == 10
        assert call_args[2].user_ids == [1, 3]

    async def test_not_found_marks_guild_not_site(self) -> None:
        api = AsyncMock()
        api.report_event_interest.side_effect = NotFoundError(404, "nope")
        cog = _make_cog(api=api)

        async def _empty_users() -> object:
            return
            yield  # pragma: no cover - makes this an async generator

        event = SimpleNamespace(id=10, guild_id=42, users=_empty_users)

        await cog._report_interest(event)

        assert 42 in cog._not_site_guild_ids


class TestDebounceInterest:
    async def test_cancels_previous_pending_task(self) -> None:
        cog = _make_cog(api=AsyncMock())
        event = SimpleNamespace(id=1, guild_id=42)

        cog._debounce_interest(event)
        first_task = cog._interest_tasks[1]
        cog._debounce_interest(event)
        second_task = cog._interest_tasks[1]
        await asyncio.sleep(0)

        assert first_task is not second_task
        assert first_task.cancelled()
        second_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await second_task

    async def test_skips_known_non_site_guild(self) -> None:
        cog = _make_cog(api=AsyncMock())
        event = SimpleNamespace(id=1, guild_id=42)
        cog._not_site_guild_ids.add(42)

        cog._debounce_interest(event)

        assert 1 not in cog._interest_tasks


class TestBuildCountsLine:
    def test_role_kind_breaks_down_by_role(self) -> None:
        push = _fake_push(
            kind=EventKind.RAID,
            counts=EventCounts(tank=2, healer=3, dps=7, signed=12, tentative=1, absent=2, interested=0),
        )
        assert build_counts_line(push) == "Going 12 (2 tanks · 3 healers · 7 DPS) · Maybe 1 · Can't 2"

    def test_non_role_kind_shows_bare_total(self) -> None:
        push = _fake_push(
            kind=EventKind.SOCIAL,
            counts=EventCounts(tank=0, healer=0, dps=0, signed=5, tentative=2, absent=1, interested=0),
        )
        assert build_counts_line(push) == "Going 5 · Maybe 2 · Can't 1"


class TestSignupCustomId:
    def test_round_trips(self) -> None:
        event_id = uuid4()
        custom_id = build_signup_custom_id(event_id, SignupStatus.TENTATIVE)
        assert custom_id == f"sq:ev:{event_id}:tentative"
        assert parse_signup_custom_id(custom_id) == (event_id, SignupStatus.TENTATIVE)

    def test_rejects_unrelated_ids(self) -> None:
        assert parse_signup_custom_id("sq:rm:some:thing") is None


class TestChangeCustomId:
    def test_round_trips(self) -> None:
        event_id = uuid4()
        custom_id = build_change_custom_id(event_id, SignupStatus.SIGNED)
        assert custom_id == f"sq:evc:{event_id}:signed"
        assert parse_change_custom_id(custom_id) == (event_id, SignupStatus.SIGNED)

    def test_rejects_unrelated_ids(self) -> None:
        assert parse_change_custom_id("sq:ev:not-a-uuid:signed") is None


class TestBuildSignupView:
    def test_has_three_persistent_buttons(self) -> None:
        view = build_signup_view(uuid4())
        assert view.timeout is None
        assert len(view.children) == 3
        labels = {button.item.label for button in view.children}
        assert labels == {"Going", "Maybe", "Can't"}


class TestGuessClassFromRoles:
    def test_matches_a_class_role_by_name(self) -> None:
        member = SimpleNamespace(roles=[SimpleNamespace(name="Warrior"), SimpleNamespace(name="Raider")])
        assert _guess_class_from_roles(member) == WowClass.WARRIOR

    def test_no_match_returns_none(self) -> None:
        member = SimpleNamespace(roles=[SimpleNamespace(name="Raider")])
        assert _guess_class_from_roles(member) is None


class TestConfirmationText:
    def test_role_kind_with_details(self) -> None:
        signup = Signup(
            id=uuid4(),
            member=SignupMember(display_name="Coffee", avatar_url=None),
            character="Oakenshield",
            wow_class=WowClass.WARRIOR,
            role=SignupRole.TANK,
            status=SignupStatus.SIGNED,
            comment=None,
            group=None,
            bench=False,
            updated_at=datetime(2099, 1, 1, tzinfo=UTC),
        )
        assert _confirmation_text(SignupStatus.SIGNED, signup) == "Going as Oakenshield · Warrior · Tank"

    def test_bare_rsvp_without_details(self) -> None:
        signup = Signup(
            id=uuid4(),
            member=SignupMember(display_name="Coffee", avatar_url=None),
            character=None,
            wow_class=None,
            role=None,
            status=SignupStatus.ABSENT,
            comment=None,
            group=None,
            bench=False,
            updated_at=datetime(2099, 1, 1, tzinfo=UTC),
        )
        assert _confirmation_text(SignupStatus.ABSENT, signup) == "Can't make it."


def _fake_interaction(*, user: Mock, api: AsyncMock, guild_id: int = 42) -> Mock:
    interaction = Mock(spec=discord.Interaction)
    interaction.guild = SimpleNamespace(id=guild_id)
    interaction.user = user
    interaction.client = SimpleNamespace(api=api)
    interaction.response = Mock()
    interaction.response.send_message = AsyncMock()
    interaction.response.send_modal = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.followup = Mock()
    interaction.followup.send = AsyncMock()
    return interaction


def _fake_signup(*, status: SignupStatus = SignupStatus.SIGNED, **overrides: object) -> Signup:
    defaults: dict[str, object] = {
        "id": uuid4(),
        "member": SignupMember(display_name="Coffee", avatar_url=None),
        "character": None,
        "wow_class": None,
        "role": None,
        "status": status,
        "comment": None,
        "group": None,
        "bench": False,
        "updated_at": datetime(2099, 1, 1, tzinfo=UTC),
    }
    defaults.update(overrides)
    return Signup.model_validate(defaults)


class TestSendSignInPrompt:
    async def test_replies_ephemerally_with_a_link_button_via_followup(self) -> None:
        """Always called after a defer (every caller defers first), so it must use the followup, not response."""
        interaction = _fake_interaction(user=Mock(), api=AsyncMock())
        await _send_sign_in_prompt(interaction)
        interaction.followup.send.assert_awaited_once()
        interaction.response.send_message.assert_not_awaited()
        _, kwargs = interaction.followup.send.call_args
        assert kwargs["ephemeral"] is True
        assert "/account/" in interaction.followup.send.call_args.args[0]
        (button,) = kwargs["view"].children
        assert button.url is not None


class TestSubmitSignup:
    async def test_no_site_account_prompts_sign_in(self) -> None:
        api = AsyncMock()
        api.upsert_event_signup_by_discord.side_effect = NotFoundError(HTTPStatus.NOT_FOUND, "nope")
        interaction = _fake_interaction(user=Mock(id=1), api=api)
        await _submit_signup(interaction, event_id=uuid4(), status=SignupStatus.SIGNED)
        interaction.followup.send.assert_awaited_once()
        assert "Sign in" in interaction.followup.send.call_args.args[0]

    async def test_event_not_scheduled_reports_closed(self) -> None:
        api = AsyncMock()
        api.upsert_event_signup_by_discord.side_effect = APIError(HTTPStatus.CONFLICT, "nope")
        interaction = _fake_interaction(user=Mock(id=1), api=api)
        await _submit_signup(interaction, event_id=uuid4(), status=SignupStatus.SIGNED)
        interaction.followup.send.assert_awaited_once_with("Sign-ups for this event are closed.", ephemeral=True)

    async def test_bare_status_sends_plain_confirmation(self) -> None:
        api = AsyncMock()
        api.upsert_event_signup_by_discord.return_value = _fake_signup(status=SignupStatus.ABSENT)
        interaction = _fake_interaction(user=Mock(id=1), api=api)
        await _submit_signup(interaction, event_id=uuid4(), status=SignupStatus.ABSENT)
        interaction.followup.send.assert_awaited_once_with("Can't make it.", ephemeral=True)

    async def test_role_kind_signup_offers_change_button(self) -> None:
        api = AsyncMock()
        api.upsert_event_signup_by_discord.return_value = _fake_signup(
            character="Oakenshield", wow_class=WowClass.WARRIOR, role=SignupRole.TANK
        )
        interaction = _fake_interaction(user=Mock(id=1), api=api)
        await _submit_signup(
            interaction,
            event_id=uuid4(),
            status=SignupStatus.SIGNED,
            character="Oakenshield",
            wow_class=WowClass.WARRIOR,
            role=SignupRole.TANK,
        )
        _, kwargs = interaction.followup.send.call_args
        assert "view" in kwargs
        (button,) = kwargs["view"].children
        assert button.item.label == "Change character or role"


class TestHandleSignupClick:
    async def test_defers_ephemerally_before_the_lookup(self) -> None:
        """Every click must ack within 3s: defer happens before the GET, not after."""
        api = AsyncMock()
        api.get_event_signup_by_discord.side_effect = NotFoundError(HTTPStatus.NOT_FOUND, "nope")
        interaction = _fake_interaction(user=MagicMock(spec=discord.Member, id=1), api=api)
        await _handle_signup_click(interaction, event_id=uuid4(), status=SignupStatus.SIGNED)
        interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)

    async def test_no_site_account_prompts_sign_in_via_followup(self) -> None:
        api = AsyncMock()
        api.get_event_signup_by_discord.side_effect = NotFoundError(HTTPStatus.NOT_FOUND, "nope")
        interaction = _fake_interaction(user=MagicMock(spec=discord.Member, id=1), api=api)
        await _handle_signup_click(interaction, event_id=uuid4(), status=SignupStatus.SIGNED)
        assert "Sign in" in interaction.followup.send.call_args.args[0]

    async def test_absent_status_submits_immediately_without_lookup_details(self) -> None:
        api = AsyncMock()
        api.get_event_signup_by_discord.return_value = SignupByDiscord(signup=None, defaults=None, kind=EventKind.RAID)
        api.upsert_event_signup_by_discord.return_value = _fake_signup(status=SignupStatus.ABSENT)
        interaction = _fake_interaction(user=MagicMock(spec=discord.Member, id=1), api=api)
        await _handle_signup_click(interaction, event_id=uuid4(), status=SignupStatus.ABSENT)
        api.upsert_event_signup_by_discord.assert_awaited_once()
        _, _, _, data = api.upsert_event_signup_by_discord.await_args.args
        assert data.character is None

    async def test_non_role_kind_bare_rsvp_submits_immediately(self) -> None:
        api = AsyncMock()
        api.get_event_signup_by_discord.return_value = SignupByDiscord(
            signup=None, defaults=None, kind=EventKind.SOCIAL
        )
        api.upsert_event_signup_by_discord.return_value = _fake_signup(status=SignupStatus.SIGNED)
        interaction = _fake_interaction(user=MagicMock(spec=discord.Member, id=1), api=api)
        await _handle_signup_click(interaction, event_id=uuid4(), status=SignupStatus.SIGNED)
        api.upsert_event_signup_by_discord.assert_awaited_once()

    async def test_role_kind_with_defaults_submits_with_prior_character(self) -> None:
        api = AsyncMock()
        api.get_event_signup_by_discord.return_value = SignupByDiscord(
            signup=None,
            defaults=SignupDefaults(character="Oakenshield", wow_class=WowClass.WARRIOR, role=SignupRole.TANK),
            kind=EventKind.RAID,
        )
        api.upsert_event_signup_by_discord.return_value = _fake_signup(
            character="Oakenshield", wow_class=WowClass.WARRIOR, role=SignupRole.TANK
        )
        interaction = _fake_interaction(user=MagicMock(spec=discord.Member, id=1), api=api)
        await _handle_signup_click(interaction, event_id=uuid4(), status=SignupStatus.SIGNED)
        api.upsert_event_signup_by_discord.assert_awaited_once()
        _, _, _, data = api.upsert_event_signup_by_discord.await_args.args
        assert data.character == "Oakenshield"

    async def test_role_kind_without_defaults_offers_enter_details_button_without_opening_a_modal(self) -> None:
        """No defaults means no modal can be opened directly (already deferred); offer a button instead."""
        api = AsyncMock()
        api.get_event_signup_by_discord.return_value = SignupByDiscord(signup=None, defaults=None, kind=EventKind.RAID)
        interaction = _fake_interaction(user=MagicMock(spec=discord.Member, roles=[], id=1), api=api)
        await _handle_signup_click(interaction, event_id=uuid4(), status=SignupStatus.SIGNED)
        interaction.response.send_modal.assert_not_awaited()
        api.upsert_event_signup_by_discord.assert_not_awaited()
        _, kwargs = interaction.followup.send.call_args
        (button,) = kwargs["view"].children
        assert button.label == "Enter character and role"


class TestEventSignupButton:
    def test_round_trips_through_from_custom_id(self) -> None:
        event_id = uuid4()
        button = EventSignupButton(event_id, SignupStatus.TENTATIVE)
        assert button.item.custom_id == build_signup_custom_id(event_id, SignupStatus.TENTATIVE)
        assert button.item.label == "Maybe"


class TestEnterDetailsView:
    async def test_opens_modal_with_no_api_calls(self) -> None:
        """The whole point: this button must open the modal with zero network round trips."""
        api = AsyncMock()
        interaction = _fake_interaction(user=MagicMock(spec=discord.Member, id=1), api=api)
        view = _EnterDetailsView(event_id=uuid4(), status=SignupStatus.SIGNED, wow_class=WowClass.WARRIOR)
        await view.enter_details.callback(interaction)
        interaction.response.send_modal.assert_awaited_once()
        (modal,) = interaction.response.send_modal.await_args.args
        assert isinstance(modal, SignupDetailsModal)
        assert modal._class_select.options[0].default == (modal._class_select.options[0].value == "warrior")
        api.get_event_signup_by_discord.assert_not_awaited()
        api.upsert_event_signup_by_discord.assert_not_awaited()


class TestChangeSignupDetailsButton:
    async def test_defers_ephemerally_before_the_lookup(self) -> None:
        api = AsyncMock()
        api.get_event_signup_by_discord.side_effect = NotFoundError(HTTPStatus.NOT_FOUND, "nope")
        button = ChangeSignupDetailsButton(uuid4(), SignupStatus.SIGNED)
        interaction = _fake_interaction(user=MagicMock(spec=discord.Member, id=1), api=api)
        await button.callback(interaction)
        interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)

    async def test_no_site_account_prompts_sign_in_via_followup(self) -> None:
        api = AsyncMock()
        api.get_event_signup_by_discord.side_effect = NotFoundError(HTTPStatus.NOT_FOUND, "nope")
        button = ChangeSignupDetailsButton(uuid4(), SignupStatus.SIGNED)
        interaction = _fake_interaction(user=MagicMock(spec=discord.Member, id=1), api=api)
        await button.callback(interaction)
        assert "Sign in" in interaction.followup.send.call_args.args[0]

    async def test_offers_enter_details_button_prefilled_with_current_signup(self) -> None:
        api = AsyncMock()
        api.get_event_signup_by_discord.return_value = SignupByDiscord(
            signup=_fake_signup(character="Oakenshield", wow_class=WowClass.WARRIOR, role=SignupRole.TANK),
            defaults=None,
            kind=EventKind.RAID,
        )
        button = ChangeSignupDetailsButton(uuid4(), SignupStatus.SIGNED)
        interaction = _fake_interaction(user=MagicMock(spec=discord.Member, id=1), api=api)
        await button.callback(interaction)
        interaction.response.send_modal.assert_not_awaited()
        _, kwargs = interaction.followup.send.call_args
        (button_item,) = kwargs["view"].children
        assert button_item.label == "Enter character and role"
        view = kwargs["view"]
        assert view.character == "Oakenshield"
        assert view.wow_class == WowClass.WARRIOR
        assert view.role == SignupRole.TANK
        # Opening it from here makes no further API call.
        await view.enter_details.callback(interaction)
        (modal,) = interaction.response.send_modal.await_args.args
        assert modal._character_input.default == "Oakenshield"
        assert api.get_event_signup_by_discord.await_count == 1
