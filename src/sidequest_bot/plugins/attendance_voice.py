"""Tracks who's in the guild's voice channels while an event runs, and pushes cumulative minutes per member.

Every ``TRACK_INTERVAL_SECONDS``: for each guild event currently running (started, not past its window end, not
cancelled), add one minute to every non-bot member currently in one of the guild's voice/stage channels, then push
that event's cumulative minutes so far. The first tick tracking a given event resumes from the API's currently
stored totals (``get_voice_presence``) rather than starting its local counter at 0, and the API keeps
``max(existing, reported)`` per member (see ``VoicePresenceReport``), so a missed tick, a bot restart, or a
duplicate push never double-counts and never lowers an already-accumulated total. The attendance screen shows
this next to each sign-up and pre-ticks "Came" at or above the threshold
(``sidequest_api.lib.attendance.VOICE_PRESENT_THRESHOLD_MINUTES``).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from discord.ext import commands, tasks

from sidequest_bot.api_client import APIError, NotFoundError
from sidequest_bot.bot import SideQuestBot  # noqa: TC001  # discord.py evaluates app-command annotations at runtime
from sidequest_common.schemas import EventStatus, VoicePresenceEntry, VoicePresenceReport

if TYPE_CHECKING:
    from collections.abc import Sequence
    from uuid import UUID

    import discord

    from sidequest_common.schemas import EventSummary

logger = logging.getLogger(__name__)

TRACK_INTERVAL_SECONDS = 60
MINUTES_PER_TICK = 1
"""Matches ``TRACK_INTERVAL_SECONDS`` (60s): one tick adds one minute."""

DEFAULT_EVENT_LENGTH = timedelta(hours=3)
"""How long an event without an explicit end counts as running, mirroring ``presence.py``."""


def running_events(events: Sequence[EventSummary], *, now: datetime) -> list[EventSummary]:
    """Events that have started, haven't passed their window end, and aren't cancelled."""
    active = []
    for event in events:
        if event.status == EventStatus.CANCELLED or event.starts_at > now:
            continue
        window_end = event.ends_at or (event.starts_at + DEFAULT_EVENT_LENGTH)
        if now <= window_end:
            active.append(event)
    return active


def voice_member_ids(guild: discord.Guild) -> set[int]:
    """Discord ids of every non-bot member currently in one of ``guild``'s voice/stage channels."""
    return {
        member.id
        for channel in (*guild.voice_channels, *guild.stage_channels)
        for member in channel.members
        if not member.bot
    }


class AttendanceVoiceCog(commands.Cog):
    """Periodic voice-presence tracking and push, per currently-running guild event."""

    def __init__(self, bot: SideQuestBot) -> None:
        self.bot = bot
        self._minutes: dict[UUID, dict[int, int]] = {}
        self._tick.start()

    async def cog_unload(self) -> None:
        """Stop the tracking loop."""
        self._tick.cancel()

    @tasks.loop(seconds=TRACK_INTERVAL_SECONDS)
    async def _tick(self) -> None:
        for guild in list(self.bot.guilds):
            await self._tick_guild(guild)

    @_tick.before_loop
    async def _before_tick(self) -> None:
        await self.bot.wait_until_ready()

    async def _resume_totals(self, guild: discord.Guild, event_id: UUID) -> dict[int, int]:
        """The event's currently stored cumulative minutes, to resume tracking instead of restarting at 0.

        A freshly-restarted process's local counter starts at 0; without this, it would (via the API's
        max-of-existing-and-reported push) stall at the already-stored total until it caught back up.
        """
        try:
            report = await self.bot.api.get_voice_presence(guild.id, event_id)
        except APIError:
            logger.warning(
                "Failed to resume voice presence totals for event %s in guild %s; starting from 0",
                event_id,
                guild.id,
                exc_info=True,
            )
            return {}
        return {int(entry.discord_id): entry.minutes for entry in report.entries}

    async def _tick_guild(self, guild: discord.Guild) -> None:
        try:
            events = await self.bot.api.list_upcoming_events()
        except APIError:
            logger.warning("Failed to list events for attendance voice tracking", exc_info=True)
            return
        now = datetime.now(UTC)
        active = running_events(events, now=now)
        present = voice_member_ids(guild) if active else set()
        for event in active:
            if event.id not in self._minutes:
                self._minutes[event.id] = await self._resume_totals(guild, event.id)
            totals = self._minutes[event.id]
            for discord_id in present:
                totals[discord_id] = totals.get(discord_id, 0) + MINUTES_PER_TICK
            await self._push(guild, event.id, totals)
        known_ids = {event.id for event in events}
        for event_id in [tracked_id for tracked_id in self._minutes if tracked_id not in known_ids]:
            del self._minutes[event_id]

    async def _push(self, guild: discord.Guild, event_id: UUID, totals: dict[int, int]) -> None:
        if not totals:
            return
        entries = [
            VoicePresenceEntry(discord_id=str(discord_id), minutes=minutes) for discord_id, minutes in totals.items()
        ]
        try:
            await self.bot.api.report_voice_presence(guild.id, VoicePresenceReport(event_id=event_id, entries=entries))
        except NotFoundError:
            # Either the API predates this route (mid rolling deploy) or the event was deleted on the site; neither
            # means the guild itself is wrong. Skip this push and let the next tick retry (or the event will drop
            # out of `_minutes` once it's no longer listed).
            logger.info("Voice presence push 404 for event %s in guild %s; will retry", event_id, guild.id)
        except APIError:
            logger.warning("Failed to push voice presence for event %s", event_id, exc_info=True)


async def setup(bot: SideQuestBot) -> None:
    """Register the cog."""
    await bot.add_cog(AttendanceVoiceCog(bot))
