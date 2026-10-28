"""Two-way raid <-> Discord scheduled event sync.

Every 15 s: reconcile new guilds once, then pull pending pushes/deletions from the API and apply them to Discord,
acking each. Discord-side listeners report creates/edits/deletes back to the API immediately. See
``local://raid-sync-contract.md`` (read at implementation time) for the full wire contract; this module is the bot
half of it. The API decides what Discord should show; this cog is the only thing that talks to Discord about it.
"""

import contextlib
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import discord
from discord.ext import commands, tasks

from sidequest_bot.api_client import APIError, NotFoundError
from sidequest_bot.bot import SideQuestBot  # noqa: TC001  # discord.py evaluates app-command annotations at runtime
from sidequest_common.schemas import (
    DiscordEventPush,
    DiscordEventReconcile,
    DiscordEventState,
    DiscordEventStatus,
    DiscordPushAck,
)

if TYPE_CHECKING:
    from collections.abc import Coroutine

logger = logging.getLogger(__name__)

SYNC_INTERVAL_SECONDS = 15

_STATUS_MAP: dict[discord.EventStatus, DiscordEventStatus] = {
    discord.EventStatus.scheduled: DiscordEventStatus.SCHEDULED,
    discord.EventStatus.active: DiscordEventStatus.ACTIVE,
    discord.EventStatus.completed: DiscordEventStatus.COMPLETED,
    discord.EventStatus.canceled: DiscordEventStatus.CANCELLED,
}

_LOCATION_ENTITY_TYPE = discord.EntityType.external
_CHANNEL_ENTITY_TYPES = (discord.EntityType.voice, discord.EntityType.stage_instance)


def _utc(value: datetime) -> datetime:
    """``value`` as an aware UTC datetime; naive values are assumed already UTC (discord.py always gives aware ones)."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def event_state(event: discord.ScheduledEvent) -> DiscordEventState:
    """The wire state for ``event``, as the API expects it."""
    return DiscordEventState(
        id=event.id,
        name=event.name,
        description=event.description,
        starts_at=_utc(event.start_time),
        ends_at=_utc(event.end_time) if event.end_time is not None else None,
        status=_STATUS_MAP[event.status],
        location=event.location if event.entity_type is _LOCATION_ENTITY_TYPE else None,
        channel_id=event.channel_id if event.entity_type in _CHANNEL_ENTITY_TYPES else None,
    )


class RaidSyncCog(commands.Cog):
    """Periodic push/pull sync plus the Discord-side listeners that report state back immediately."""

    def __init__(self, bot: SideQuestBot) -> None:
        self.bot = bot
        self._reconciled_guild_ids: set[int] = set()
        self._not_site_guild_ids: set[int] = set()
        self._forbidden_guild_ids: set[int] = set()
        self._sync_loop.start()

    async def cog_unload(self) -> None:
        """Stop the sync loop when the cog is unloaded."""
        self._sync_loop.cancel()

    # --- Periodic push/pull ---------------------------------------------------------------------------------------

    @tasks.loop(seconds=SYNC_INTERVAL_SECONDS)
    async def _sync_loop(self) -> None:
        for guild in list(self.bot.guilds):
            if guild.id in self._not_site_guild_ids:
                continue
            try:
                if guild.id not in self._reconciled_guild_ids:
                    await self._reconcile(guild)
                    self._reconciled_guild_ids.add(guild.id)
                await self._sync_guild(guild)
            except NotFoundError:
                logger.info("Guild %s is not the site guild; no longer syncing raids there", guild.id)
                self._not_site_guild_ids.add(guild.id)
            except APIError:
                logger.warning("Raid sync failed for guild %s", guild.id, exc_info=True)

    @_sync_loop.before_loop
    async def _before_sync_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def _reconcile(self, guild: discord.Guild) -> None:
        """Report every scheduled event currently in ``guild``, once per process."""
        events = await guild.fetch_scheduled_events()
        reconcile = DiscordEventReconcile(events=[event_state(event) for event in events])
        await self.bot.api.reconcile_raid_sync(guild.id, reconcile)

    async def _sync_guild(self, guild: discord.Guild) -> None:
        """Apply one cycle's worth of pending deletions and pushes, acking/clearing each as it lands."""
        pending = await self.bot.api.get_pending_raid_sync(guild.id)
        for event_id in pending.deletions:
            if not await self._apply_one(guild, self._apply_deletion(guild, event_id)):
                return
        for push in pending.pushes:
            if not await self._apply_one(guild, self._apply_push(guild, push)):
                return

    async def _apply_one(self, guild: discord.Guild, action: Coroutine[Any, Any, None]) -> bool:
        """Run one push/deletion coroutine. ``False`` means "stop the cycle for this guild" (missing permission)."""
        try:
            await action
        except discord.Forbidden:
            if guild.id not in self._forbidden_guild_ids:
                logger.warning(
                    "Missing Manage Events permission in guild %s; raid sync skipped until it's granted", guild.id
                )
                self._forbidden_guild_ids.add(guild.id)
            return False
        except discord.HTTPException, APIError:
            logger.warning("A raid-sync push failed in guild %s", guild.id, exc_info=True)
            return True
        else:
            self._forbidden_guild_ids.discard(guild.id)
            return True

    async def _apply_deletion(self, guild: discord.Guild, event_id: int) -> None:
        """Delete ``event_id`` in Discord (404 is fine), then clear its tombstone."""
        try:
            event = await guild.fetch_scheduled_event(event_id)
        except discord.NotFound:
            pass
        else:
            with contextlib.suppress(discord.NotFound):
                await event.delete()
        await self.bot.api.delete_raid_sync_tombstone(guild.id, event_id)

    async def _apply_push(self, guild: discord.Guild, push: DiscordEventPush) -> None:
        """Apply one push and ack it with whatever Discord now holds (``None`` if nothing is linked any more)."""
        if push.action == "upsert":
            state = await self._apply_upsert(guild, push)
        else:
            state = await self._apply_cancel_or_complete(guild, push)
        ack = DiscordPushAck(push_hash=push.push_hash, event=state)
        await self.bot.api.ack_raid_push(guild.id, push.raid_id, ack)

    async def _apply_upsert(self, guild: discord.Guild, push: DiscordEventPush) -> DiscordEventState | None:
        event = await self._fetch_event(guild, push.discord_event_id)
        if event is None:
            if _utc(push.starts_at) <= discord.utils.utcnow():
                return None
            created = await guild.create_scheduled_event(
                name=push.name,
                description=push.description,
                start_time=_utc(push.starts_at),
                end_time=_utc(push.ends_at),
                entity_type=discord.EntityType.external,
                privacy_level=discord.PrivacyLevel.guild_only,
                location=push.location,
            )
            return event_state(created)
        if event.status is discord.EventStatus.scheduled:
            edit_kwargs: dict[str, Any] = {
                "name": push.name,
                "description": push.description,
                "start_time": _utc(push.starts_at),
                "end_time": _utc(push.ends_at),
            }
            if event.entity_type is _LOCATION_ENTITY_TYPE:
                edit_kwargs["location"] = push.location
            edited = await event.edit(**edit_kwargs)
            return event_state(edited)
        if event.status is discord.EventStatus.active:
            edited = await event.edit(name=push.name, description=push.description, end_time=_utc(push.ends_at))
            return event_state(edited)
        return None  # completed/cancelled

    async def _apply_cancel_or_complete(self, guild: discord.Guild, push: DiscordEventPush) -> None:
        event = await self._fetch_event(guild, push.discord_event_id)
        if event is None:
            return
        if push.action == "cancel":
            if event.status is discord.EventStatus.scheduled:
                await event.cancel()
            elif event.status is discord.EventStatus.active:
                await event.end()
            return
        if event.status is discord.EventStatus.active:
            await event.end()
        elif event.status is discord.EventStatus.scheduled:
            await event.delete()

    async def _fetch_event(self, guild: discord.Guild, event_id: int | None) -> discord.ScheduledEvent | None:
        """``event_id``'s current state, or ``None`` if it's unset or 404s."""
        if event_id is None:
            return None
        try:
            return await guild.fetch_scheduled_event(event_id)
        except discord.NotFound:
            return None

    # --- Discord-side listeners ------------------------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_scheduled_event_create(self, event: discord.ScheduledEvent) -> None:
        """Report a Discord-created event to the API right away."""
        await self._report_event(event)

    @commands.Cog.listener()
    async def on_scheduled_event_update(self, _before: discord.ScheduledEvent, after: discord.ScheduledEvent) -> None:
        """Report the event's new state."""
        await self._report_event(after)

    @commands.Cog.listener()
    async def on_scheduled_event_delete(self, event: discord.ScheduledEvent) -> None:
        """Report that ``event`` was deleted in Discord."""
        if event.guild_id in self._not_site_guild_ids:
            return
        try:
            await self.bot.api.report_discord_event_deleted(event.guild_id, event.id)
        except NotFoundError:
            self._not_site_guild_ids.add(event.guild_id)
        except APIError:
            logger.warning(
                "Failed to report deleted scheduled event %s in guild %s", event.id, event.guild_id, exc_info=True
            )

    async def _report_event(self, event: discord.ScheduledEvent) -> None:
        if event.guild_id in self._not_site_guild_ids:
            return
        try:
            await self.bot.api.report_discord_event(event.guild_id, event_state(event))
        except NotFoundError:
            self._not_site_guild_ids.add(event.guild_id)
        except APIError:
            logger.warning("Failed to report scheduled event %s in guild %s", event.id, event.guild_id, exc_info=True)


async def setup(bot: SideQuestBot) -> None:
    """Register the cog."""
    await bot.add_cog(RaidSyncCog(bot))
