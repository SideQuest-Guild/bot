"""Unit tests for the attendance-voice plugin: running_events, voice_member_ids, and the tracking/push loop."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import discord

from sidequest_bot.api_client import APIError, NotFoundError
from sidequest_bot.plugins.attendance_voice import AttendanceVoiceCog, running_events, voice_member_ids
from sidequest_common.schemas import EventKind, EventStatus, EventSummary, VoicePresenceEntry, VoicePresenceReport


def _event(
    *,
    starts_at: datetime,
    ends_at: datetime | None = None,
    status: EventStatus = EventStatus.SCHEDULED,
    kind: EventKind = EventKind.RAID,
) -> EventSummary:
    return EventSummary(
        id=uuid4(),
        kind=kind,
        title="Molten Core",
        location="",
        starts_at=starts_at,
        ends_at=ends_at,
        size=None,
        ep=10,
        ep_override=None,
        status=status,
        notes=None,
        counts=None,
        series=None,
    )


def _fake_voice_channel(*, members: list[MagicMock]) -> MagicMock:
    channel = MagicMock(spec=discord.VoiceChannel)
    channel.members = members
    return channel


def _fake_member(*, member_id: int, bot: bool = False) -> MagicMock:
    member = MagicMock(spec=discord.Member)
    member.id = member_id
    member.bot = bot
    return member


def _fake_guild(*, guild_id: int = 42, voice_channels: list[MagicMock] | None = None) -> MagicMock:
    guild = MagicMock(spec=discord.Guild)
    guild.id = guild_id
    guild.voice_channels = voice_channels or []
    guild.stage_channels = []
    return guild


def _make_cog(*, api: object, guilds: list[object] | None = None) -> AttendanceVoiceCog:
    cog = AttendanceVoiceCog.__new__(AttendanceVoiceCog)
    cog.bot = SimpleNamespace(api=api, guilds=guilds or [])
    cog._minutes = {}
    if isinstance(api, AsyncMock):
        api.get_voice_presence.side_effect = lambda _guild_id, event_id: VoicePresenceReport(
            event_id=event_id, entries=[]
        )
    return cog


class TestRunningEvents:
    def test_excludes_future_events(self) -> None:
        now = datetime.now(UTC)
        event = _event(starts_at=now + timedelta(hours=1))
        assert running_events([event], now=now) == []

    def test_excludes_cancelled_events(self) -> None:
        now = datetime.now(UTC)
        event = _event(starts_at=now - timedelta(minutes=10), status=EventStatus.CANCELLED)
        assert running_events([event], now=now) == []

    def test_includes_started_event_within_explicit_end(self) -> None:
        now = datetime.now(UTC)
        event = _event(starts_at=now - timedelta(minutes=30), ends_at=now + timedelta(minutes=30))
        assert running_events([event], now=now) == [event]

    def test_excludes_event_past_its_explicit_end(self) -> None:
        now = datetime.now(UTC)
        event = _event(starts_at=now - timedelta(hours=2), ends_at=now - timedelta(minutes=1))
        assert running_events([event], now=now) == []

    def test_uses_default_three_hour_window_without_explicit_end(self) -> None:
        now = datetime.now(UTC)
        within_default = _event(starts_at=now - timedelta(hours=2))
        past_default = _event(starts_at=now - timedelta(hours=4))
        assert running_events([within_default, past_default], now=now) == [within_default]


class TestVoiceMemberIds:
    def test_excludes_bots(self) -> None:
        human = _fake_member(member_id=1)
        bot_member = _fake_member(member_id=2, bot=True)
        guild = _fake_guild(voice_channels=[_fake_voice_channel(members=[human, bot_member])])
        assert voice_member_ids(guild) == {1}

    def test_merges_members_across_channels(self) -> None:
        guild = _fake_guild(
            voice_channels=[
                _fake_voice_channel(members=[_fake_member(member_id=1)]),
                _fake_voice_channel(members=[_fake_member(member_id=2)]),
            ]
        )
        assert voice_member_ids(guild) == {1, 2}

    def test_empty_guild_has_no_voice_members(self) -> None:
        assert voice_member_ids(_fake_guild()) == set()


class TestTickGuild:
    async def test_adds_a_minute_for_everyone_present_and_pushes(self) -> None:
        now = datetime.now(UTC)
        event = _event(starts_at=now - timedelta(minutes=10))
        guild = _fake_guild(voice_channels=[_fake_voice_channel(members=[_fake_member(member_id=1)])])
        api = AsyncMock()
        api.list_upcoming_events.return_value = [event]
        cog = _make_cog(api=api, guilds=[guild])

        await cog._tick_guild(guild)

        api.report_voice_presence.assert_awaited_once_with(
            guild.id, VoicePresenceReport(event_id=event.id, entries=[VoicePresenceEntry(discord_id="1", minutes=1)])
        )

    async def test_minutes_accumulate_across_ticks(self) -> None:
        now = datetime.now(UTC)
        event = _event(starts_at=now - timedelta(minutes=10))
        guild = _fake_guild(voice_channels=[_fake_voice_channel(members=[_fake_member(member_id=1)])])
        api = AsyncMock()
        api.list_upcoming_events.return_value = [event]
        cog = _make_cog(api=api, guilds=[guild])

        await cog._tick_guild(guild)
        await cog._tick_guild(guild)

        last_call = api.report_voice_presence.await_args_list[-1]
        report: VoicePresenceReport = last_call.args[1]
        assert report.entries == [VoicePresenceEntry(discord_id="1", minutes=2)]

    async def test_repeated_pushes_with_unchanged_presence_are_idempotent_on_the_api_side(self) -> None:
        """Each push carries the cumulative total so far (not a delta), so a retried/duplicate push is a no-op."""
        now = datetime.now(UTC)
        event = _event(starts_at=now - timedelta(minutes=10))
        guild = _fake_guild(voice_channels=[_fake_voice_channel(members=[_fake_member(member_id=1)])])
        api = AsyncMock()
        api.list_upcoming_events.return_value = [event]
        cog = _make_cog(api=api, guilds=[guild])
        await cog._tick_guild(guild)
        first_entries = api.report_voice_presence.await_args_list[0].args[1].entries

        # Simulate retrying the same push (e.g. a transient failure before the caller saw success): the cog always
        # sends the full cumulative dict, so pushing the same totals twice in a row yields the same wire payload.
        await cog._push(guild, event.id, cog._minutes[event.id])
        second_entries = api.report_voice_presence.await_args_list[-1].args[1].entries

        assert first_entries == second_entries

    async def test_no_running_events_does_not_push(self) -> None:
        now = datetime.now(UTC)
        event = _event(starts_at=now + timedelta(hours=1))
        guild = _fake_guild(voice_channels=[_fake_voice_channel(members=[_fake_member(member_id=1)])])
        api = AsyncMock()
        api.list_upcoming_events.return_value = [event]
        cog = _make_cog(api=api, guilds=[guild])

        await cog._tick_guild(guild)

        api.report_voice_presence.assert_not_awaited()

    async def test_event_no_longer_listed_is_pruned_from_memory(self) -> None:
        now = datetime.now(UTC)
        event = _event(starts_at=now - timedelta(minutes=10))
        guild = _fake_guild(voice_channels=[_fake_voice_channel(members=[_fake_member(member_id=1)])])
        api = AsyncMock()
        api.list_upcoming_events.return_value = [event]
        cog = _make_cog(api=api, guilds=[guild])
        await cog._tick_guild(guild)
        assert event.id in cog._minutes

        api.list_upcoming_events.return_value = []
        await cog._tick_guild(guild)

        assert event.id not in cog._minutes

    async def test_voice_push_404_does_not_disable_tracking(self) -> None:
        """Old API mid-deploy or a deleted event 404s the push; the guild/event keep being tracked for retry."""
        now = datetime.now(UTC)
        event = _event(starts_at=now - timedelta(minutes=10))
        guild = _fake_guild(voice_channels=[_fake_voice_channel(members=[_fake_member(member_id=1)])])
        api = AsyncMock()
        api.list_upcoming_events.return_value = [event]
        api.report_voice_presence.side_effect = NotFoundError(404, "not found")
        cog = _make_cog(api=api, guilds=[guild])

        await cog._tick_guild(guild)  # must not raise

        assert event.id in cog._minutes  # still tracked, so the next tick retries

    async def test_voice_push_404_retries_on_the_next_tick(self) -> None:
        now = datetime.now(UTC)
        event = _event(starts_at=now - timedelta(minutes=10))
        guild = _fake_guild(voice_channels=[_fake_voice_channel(members=[_fake_member(member_id=1)])])
        api = AsyncMock()
        api.list_upcoming_events.return_value = [event]
        api.report_voice_presence.side_effect = [NotFoundError(404, "not found"), None]
        cog = _make_cog(api=api, guilds=[guild])

        await cog._tick_guild(guild)
        await cog._tick_guild(guild)

        assert api.report_voice_presence.await_count == 2

    async def test_other_api_errors_do_not_crash_the_tick(self) -> None:
        now = datetime.now(UTC)
        event = _event(starts_at=now - timedelta(minutes=10))
        guild = _fake_guild(voice_channels=[_fake_voice_channel(members=[_fake_member(member_id=1)])])
        api = AsyncMock()
        api.list_upcoming_events.return_value = [event]
        api.report_voice_presence.side_effect = APIError(500, "boom")
        cog = _make_cog(api=api, guilds=[guild])

        await cog._tick_guild(guild)  # must not raise

        assert event.id in cog._minutes

    async def test_list_events_failure_does_not_crash_the_tick(self) -> None:
        guild = _fake_guild()
        api = AsyncMock()
        api.list_upcoming_events.side_effect = APIError(500, "boom")
        cog = _make_cog(api=api, guilds=[guild])

        await cog._tick_guild(guild)  # must not raise

        api.report_voice_presence.assert_not_awaited()


class TestVoicePresenceResume:
    """A bot restart resets ``_minutes`` to ``{}``; the first tick for a still-running event must resume from the
    API's stored totals instead of reporting a lower total that could un-suggest "Came" on the attendance screen.
    """

    async def test_first_tick_for_a_tracked_event_resumes_from_stored_totals(self) -> None:
        now = datetime.now(UTC)
        event = _event(starts_at=now - timedelta(minutes=95))
        guild = _fake_guild(voice_channels=[_fake_voice_channel(members=[_fake_member(member_id=1)])])
        api = AsyncMock()
        api.list_upcoming_events.return_value = [event]
        cog = _make_cog(api=api, guilds=[guild])  # fresh process: cog._minutes starts empty
        api.get_voice_presence.side_effect = None
        api.get_voice_presence.return_value = VoicePresenceReport(
            event_id=event.id, entries=[VoicePresenceEntry(discord_id="1", minutes=90)]
        )

        await cog._tick_guild(guild)

        api.get_voice_presence.assert_awaited_once_with(guild.id, event.id)
        report: VoicePresenceReport = api.report_voice_presence.await_args.args[1]
        assert report.entries == [VoicePresenceEntry(discord_id="1", minutes=91)]

    async def test_resume_is_only_fetched_once_per_tracked_event(self) -> None:
        now = datetime.now(UTC)
        event = _event(starts_at=now - timedelta(minutes=10))
        guild = _fake_guild(voice_channels=[_fake_voice_channel(members=[_fake_member(member_id=1)])])
        api = AsyncMock()
        api.list_upcoming_events.return_value = [event]
        cog = _make_cog(api=api, guilds=[guild])

        await cog._tick_guild(guild)
        await cog._tick_guild(guild)

        api.get_voice_presence.assert_awaited_once()

    async def test_resume_failure_falls_back_to_zero_without_crashing(self) -> None:
        now = datetime.now(UTC)
        event = _event(starts_at=now - timedelta(minutes=10))
        guild = _fake_guild(voice_channels=[_fake_voice_channel(members=[_fake_member(member_id=1)])])
        api = AsyncMock()
        api.list_upcoming_events.return_value = [event]
        cog = _make_cog(api=api, guilds=[guild])
        api.get_voice_presence.side_effect = APIError(500, "boom")

        await cog._tick_guild(guild)  # must not raise

        report: VoicePresenceReport = api.report_voice_presence.await_args.args[1]
        assert report.entries == [VoicePresenceEntry(discord_id="1", minutes=1)]
