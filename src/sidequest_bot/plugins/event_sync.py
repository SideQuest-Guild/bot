"""Two-way guild event <-> Discord scheduled event sync, Discord "Interested" reporting, and the events forum.

Every 15 s: reconcile new guilds once, then pull pending pushes/deletions/activity from the API and apply them to
Discord, acking each. Discord-side listeners report creates/edits/deletes back to the API immediately, and
debounced ``on_scheduled_event_user_add``/``_remove`` listeners report Discord "Interested" clicks. See
``local://event-forum-contract.md`` (read at implementation time) for the full wire contract; this module is the
bot half of it. The API decides what Discord should show; this cog is the only thing that talks to Discord about it.

When a forum channel is configured (``/eventsync forum``), every upcoming push also gets a forum post: one thread
per guild event, tagged by kind, edited in place as the event changes, archived+locked on cancel/complete/deletion,
and commented on as members sign up, go tentative, decline, or withdraw.
"""

import asyncio
import contextlib
import logging
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import discord
from discord import app_commands
from discord.ext import commands, tasks

from sidequest_bot.api_client import APIError, NotFoundError
from sidequest_bot.bot import SideQuestBot  # noqa: TC001  # discord.py evaluates app-command annotations at runtime
from sidequest_common.schemas import (
    EVENT_KIND_LABELS,
    DiscordEventPush,
    DiscordEventReconcile,
    DiscordEventState,
    DiscordEventStatus,
    DiscordInterestReport,
    DiscordPushAck,
    EventKind,
    GuildConfigUpdate,
    SignupActivity,
)

if TYPE_CHECKING:
    from collections.abc import Coroutine, Sequence
    from uuid import UUID

logger = logging.getLogger(__name__)

SYNC_INTERVAL_SECONDS = 15
INTEREST_DEBOUNCE_SECONDS = 5
MAX_FORUM_STARTER_LENGTH = 2000

_STATUS_MAP: dict[discord.EventStatus, DiscordEventStatus] = {
    discord.EventStatus.scheduled: DiscordEventStatus.SCHEDULED,
    discord.EventStatus.active: DiscordEventStatus.ACTIVE,
    discord.EventStatus.completed: DiscordEventStatus.COMPLETED,
    discord.EventStatus.canceled: DiscordEventStatus.CANCELLED,
}

_LOCATION_ENTITY_TYPE = discord.EntityType.external
_CHANNEL_ENTITY_TYPES = (discord.EntityType.voice, discord.EntityType.stage_instance)

# Matches the API's footer marker (``sidequest_api.domain.event_sync.services.FOOTER_HEADER``); used only to strip
# the footer back out of ``push.description`` for the forum starter post, which composes its own layout instead.
_DESCRIPTION_FOOTER_HEADER = "── SideQuest event ──"

_ACTIVITY_EMOJI = {"going": "✅", "maybe": "❔", "not_going": "❌", "withdrew": "↩️"}
_ACTIVITY_VERB = {"going": "is going", "maybe": "might make it", "not_going": "can't make it", "withdrew": "withdrew"}
# Discord markdown characters (plus a line-leading list dash) in a member's sign-up comment, escaped so it shows as
# typed. discord.utils.escape_markdown would do, but it trips a DeprecationWarning on Python 3.15.
_COMMENT_MARKDOWN = re.compile(r"[\\*_~`|>#\[\]]|^-", re.MULTILINE)


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


def _truncate(text: str, limit: int) -> str:
    """``text`` cut to ``limit`` chars, with a trailing "…" if it was cut."""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit == 1:
        return "…"
    return text[: limit - 1] + "…"


def _strip_description_footer(description: str) -> str:
    """``description`` with the API's "── SideQuest event ──" footer (and anything after it) removed."""
    index = description.find(_DESCRIPTION_FOOTER_HEADER)
    notes = description if index == -1 else description[:index]
    return notes.strip()


def build_starter_message(push: DiscordEventPush, *, guild_id: int, discord_event_id: int) -> str:
    """Forum starter-post content for ``push``, trimmed to fit Discord's 2000-char message limit.

    Layout: kind · location, the start (and end) time as Discord timestamps, the event's notes (trimmed to fit),
    the sign-up link, and a link back to the Discord event.
    """
    header_parts = [EVENT_KIND_LABELS[push.kind]]
    if push.location:
        header_parts.append(push.location)
    header = " · ".join(header_parts)
    time_line = f"<t:{int(push.starts_at.timestamp())}:F>"
    time_line += f" - <t:{int(push.ends_at.timestamp())}:F>"
    links = "\n".join(
        [f"Sign up: {push.signup_url}", f"Discord event: https://discord.com/events/{guild_id}/{discord_event_id}"]
    )
    skeleton = f"{header}\n\n{time_line}\n\n{links}"
    notes = _strip_description_footer(push.description)
    if not notes:
        return skeleton
    budget = MAX_FORUM_STARTER_LENGTH - len(skeleton) - 2
    trimmed_notes = _truncate(notes, budget) if budget > 0 else ""
    if not trimmed_notes:
        return skeleton
    return f"{header}\n\n{time_line}\n\n{trimmed_notes}\n\n{links}"


def build_activity_line(activity: SignupActivity) -> str:
    """One forum comment for ``activity``, e.g. "✅ <@123> is going — Oakenshield · Warrior · Tank".

    The member is @-mentioned when the row carries their Discord id (it links to them; the send passes
    ``AllowedMentions.none()`` so nobody is pinged), else their name is bolded. A sign-up comment follows as a quote,
    markdown-escaped so it shows as typed.
    """
    who = f"<@{activity.discord_id}>" if activity.discord_id is not None else f"**{activity.display_name}**"
    line = f"{_ACTIVITY_EMOJI[activity.action]} {who} {_ACTIVITY_VERB[activity.action]}"
    if activity.detail:
        line += f" — {activity.detail}"
    if activity.comment and activity.comment.strip():
        quoted = _COMMENT_MARKDOWN.sub(lambda m: "\\" + m.group(0), activity.comment.strip()).splitlines()
        line += "".join(f"\n> {text}" for text in quoted)
    return line


def _find_tag(tags: Sequence[discord.ForumTag], kind: EventKind) -> discord.ForumTag | None:
    """The forum tag whose name matches ``kind``'s label, if the forum already has one."""
    label = EVENT_KIND_LABELS[kind]
    return next((tag for tag in tags if tag.name == label), None)


class EventSyncCog(commands.Cog):
    """Periodic push/pull sync, the forum post lifecycle, interest debouncing, and the ``/eventsync`` group."""

    eventsync_group = app_commands.Group(
        name="eventsync",
        description="Configure the events forum.",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    def __init__(self, bot: SideQuestBot) -> None:
        self.bot = bot
        self._reconciled_guild_ids: set[int] = set()
        self._not_site_guild_ids: set[int] = set()
        self._forbidden_guild_ids: set[int] = set()
        self._forum_forbidden_guild_ids: set[int] = set()
        self._tag_forbidden_guild_ids: set[int] = set()
        # Discord scheduled event id -> forum thread id, filled in as pushes are applied. Lets a later tombstone
        # deletion (which only carries the scheduled event id) find and close the thread it can no longer ack.
        self._thread_ids: dict[int, int] = {}
        self._interest_tasks: dict[int, asyncio.Task[None]] = {}
        self._sync_loop.start()

    async def cog_unload(self) -> None:
        """Stop the sync loop and any debounced interest reports when the cog is unloaded."""
        self._sync_loop.cancel()
        for task in self._interest_tasks.values():
            task.cancel()

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
                logger.info("Guild %s is not the site guild; no longer syncing events there", guild.id)
                self._not_site_guild_ids.add(guild.id)
            except APIError:
                logger.warning("Event sync failed for guild %s", guild.id, exc_info=True)

    @_sync_loop.before_loop
    async def _before_sync_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def _reconcile(self, guild: discord.Guild) -> None:
        """Report every scheduled event currently in ``guild``, once per process, plus its current interest."""
        events = await guild.fetch_scheduled_events()
        reconcile = DiscordEventReconcile(events=[event_state(event) for event in events])
        await self.bot.api.reconcile_event_sync(guild.id, reconcile)
        for event in events:
            await self._report_interest(event)

    async def _sync_guild(self, guild: discord.Guild) -> None:
        """Apply one cycle's worth of pending deletions, pushes, and activity comments."""
        pending = await self.bot.api.get_pending_event_sync(guild.id)
        for event_id in pending.deletions:
            if not await self._apply_one(guild, self._apply_deletion(guild, event_id)):
                return
        for push in pending.pushes:
            if not await self._apply_one(guild, self._apply_push(guild, push)):
                return
        await self._post_activity(guild, pending.activity)

    async def _apply_one(self, guild: discord.Guild, action: Coroutine[Any, Any, None]) -> bool:
        """Run one push/deletion coroutine. ``False`` means "stop the cycle for this guild" (missing permission)."""
        try:
            await action
        except discord.Forbidden:
            if guild.id not in self._forbidden_guild_ids:
                logger.warning(
                    "Missing Manage Events permission in guild %s; event sync skipped until it's granted", guild.id
                )
                self._forbidden_guild_ids.add(guild.id)
            return False
        except discord.HTTPException, APIError:
            logger.warning("An event-sync push failed in guild %s", guild.id, exc_info=True)
            return True
        else:
            self._forbidden_guild_ids.discard(guild.id)
            return True

    async def _apply_deletion(self, guild: discord.Guild, event_id: int) -> None:
        """Delete ``event_id`` in Discord (404 is fine), close its forum thread if we know of one, clear tombstone."""
        try:
            event = await guild.fetch_scheduled_event(event_id)
        except discord.NotFound:
            pass
        else:
            with contextlib.suppress(discord.NotFound):
                await event.delete()
        await self._close_forum_thread_for_deletion(guild, event_id)
        await self.bot.api.delete_event_sync_tombstone(guild.id, event_id)

    async def _apply_push(self, guild: discord.Guild, push: DiscordEventPush) -> None:
        """Apply one push, ensure its forum post, and ack with whatever Discord now holds and the thread id."""
        if push.action == "upsert":
            state = await self._apply_upsert(guild, push)
        else:
            state = await self._apply_cancel_or_complete(guild, push)
        effective_discord_id = state.id if state is not None else push.discord_event_id
        thread_id = await self._ensure_forum_post(guild, push, state, effective_discord_id)
        if thread_id is not None and effective_discord_id is not None:
            self._thread_ids[effective_discord_id] = thread_id
        ack = DiscordPushAck(push_hash=push.push_hash, event=state, thread_id=thread_id)
        await self.bot.api.ack_event_push(guild.id, push.event_id, ack)

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

    # --- Events forum -----------------------------------------------------------------------------------------

    async def _ensure_forum_post(
        self,
        guild: discord.Guild,
        push: DiscordEventPush,
        state: DiscordEventState | None,
        discord_event_id: int | None,
    ) -> int | None:
        """Create/update/close the forum thread for ``push``.

        Returns the thread id to ack back (``push.thread_id`` unchanged if no forum is configured or the event
        isn't in a state that needs forum work).
        """
        config = await self.bot.configs.get(guild.id)
        if config.events_forum_channel_id is None:
            return push.thread_id
        forum = guild.get_channel(config.events_forum_channel_id)
        if not isinstance(forum, discord.ForumChannel):
            return push.thread_id
        if push.action == "upsert":
            if state is None or discord_event_id is None:
                return push.thread_id
            return await self._ensure_upsert_thread(forum, push, discord_event_id)
        return await self._ensure_cancel_or_complete_thread(forum, push)

    async def _ensure_upsert_thread(
        self, forum: discord.ForumChannel, push: DiscordEventPush, discord_event_id: int
    ) -> int | None:
        name = push.title[:100]
        content = build_starter_message(push, guild_id=forum.guild.id, discord_event_id=discord_event_id)
        if push.thread_id is None:
            tag = await self._ensure_tag(forum, push.kind)
            try:
                created = await forum.create_thread(name=name, content=content, applied_tags=[tag] if tag else [])
            except discord.Forbidden:
                self._warn_forum_forbidden(forum.guild.id)
                return None
            return created.thread.id
        thread = await self._fetch_thread(forum.guild, push.thread_id)
        if thread is None:
            return None
        try:
            await thread.edit(name=name)
            starter = await thread.fetch_message(thread.id)
            await starter.edit(content=content)
        except discord.Forbidden:
            self._warn_forum_forbidden(forum.guild.id)
        except discord.NotFound:
            pass
        return push.thread_id

    async def _ensure_cancel_or_complete_thread(
        self, forum: discord.ForumChannel, push: DiscordEventPush
    ) -> int | None:
        if push.thread_id is None:
            return None
        thread = await self._fetch_thread(forum.guild, push.thread_id)
        if thread is None:
            return None
        try:
            if push.action == "cancel":
                await thread.send("This event was cancelled.", allowed_mentions=discord.AllowedMentions.none())
            await thread.edit(archived=True, locked=True)
        except discord.Forbidden:
            self._warn_forum_forbidden(forum.guild.id)
        except discord.NotFound:
            pass
        return push.thread_id

    async def _close_forum_thread_for_deletion(self, guild: discord.Guild, event_id: int) -> None:
        """If we know of a forum thread for the now-deleted ``event_id``, post removal notice and archive+lock it."""
        thread_id = self._thread_ids.pop(event_id, None)
        if thread_id is None:
            return
        thread = await self._fetch_thread(guild, thread_id)
        if thread is None:
            return
        try:
            await thread.send("This event was removed.", allowed_mentions=discord.AllowedMentions.none())
            await thread.edit(archived=True, locked=True)
        except discord.Forbidden:
            self._warn_forum_forbidden(guild.id)
        except discord.NotFound:
            pass

    async def _ensure_tag(self, forum: discord.ForumChannel, kind: EventKind) -> discord.ForumTag | None:
        """The forum tag for ``kind``, creating it if missing. ``None`` (and a one-time warning) if forbidden."""
        existing = _find_tag(forum.available_tags, kind)
        if existing is not None:
            return existing
        try:
            return await forum.create_tag(name=EVENT_KIND_LABELS[kind])
        except discord.Forbidden:
            if forum.guild.id not in self._tag_forbidden_guild_ids:
                logger.warning(
                    "Missing Manage Channels permission to create forum tags in guild %s; posting without tags",
                    forum.guild.id,
                )
                self._tag_forbidden_guild_ids.add(forum.guild.id)
            return None

    async def _fetch_thread(self, guild: discord.Guild, thread_id: int) -> discord.Thread | None:
        """``thread_id``'s current state, or ``None`` if it's gone."""
        thread = guild.get_thread(thread_id)
        if thread is not None:
            return thread
        try:
            channel = await guild.fetch_channel(thread_id)
        except discord.NotFound:
            return None
        return channel if isinstance(channel, discord.Thread) else None

    def _warn_forum_forbidden(self, guild_id: int) -> None:
        if guild_id not in self._forum_forbidden_guild_ids:
            logger.warning("Missing permission to manage the events forum in guild %s", guild_id)
            self._forum_forbidden_guild_ids.add(guild_id)

    # --- Sign-up activity -> forum comments --------------------------------------------------------------------

    async def _post_activity(self, guild: discord.Guild, activity: list[SignupActivity]) -> None:
        """Post one comment per unposted activity row, then ack whichever rows are settled (posted or undeliverable)."""
        posted_ids: list[UUID] = []
        for row in activity:
            thread = await self._fetch_thread(guild, row.thread_id)
            if thread is not None:
                try:
                    await thread.send(build_activity_line(row), allowed_mentions=discord.AllowedMentions.none())
                except discord.NotFound:
                    pass
                except discord.Forbidden:
                    self._warn_forum_forbidden(guild.id)
                    continue
            posted_ids.append(row.id)
        if not posted_ids:
            return
        try:
            await self.bot.api.ack_event_activity(guild.id, posted_ids)
        except APIError:
            logger.warning("Failed to ack sign-up activity for guild %s", guild.id, exc_info=True)

    # --- Discord "Interested" -> site --------------------------------------------------------------------------

    async def _report_interest(self, event: discord.ScheduledEvent) -> None:
        """Report the full current interested-user list for ``event`` (no-op on the API if it isn't linked)."""
        user_ids = [user.id async for user in event.users() if not user.bot]
        try:
            await self.bot.api.report_event_interest(event.guild_id, event.id, DiscordInterestReport(user_ids=user_ids))
        except NotFoundError:
            self._not_site_guild_ids.add(event.guild_id)
        except APIError:
            logger.warning(
                "Failed to report interest for scheduled event %s in guild %s", event.id, event.guild_id, exc_info=True
            )

    def _debounce_interest(self, event: discord.ScheduledEvent) -> None:
        if event.guild_id in self._not_site_guild_ids:
            return
        pending = self._interest_tasks.get(event.id)
        if pending is not None and not pending.done():
            pending.cancel()
        self._interest_tasks[event.id] = asyncio.create_task(self._debounced_report_interest(event))

    async def _debounced_report_interest(self, event: discord.ScheduledEvent) -> None:
        try:
            await asyncio.sleep(INTEREST_DEBOUNCE_SECONDS)
        except asyncio.CancelledError:
            return
        await self._report_interest(event)

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

    @commands.Cog.listener()
    async def on_scheduled_event_user_add(self, event: discord.ScheduledEvent, user: discord.User) -> None:
        """A user clicked "Interested"; debounce and report the full list shortly."""
        del user
        self._debounce_interest(event)

    @commands.Cog.listener()
    async def on_scheduled_event_user_remove(self, event: discord.ScheduledEvent, user: discord.User) -> None:
        """A user un-clicked "Interested"; debounce and report the full list shortly."""
        del user
        self._debounce_interest(event)

    async def _report_event(self, event: discord.ScheduledEvent) -> None:
        if event.guild_id in self._not_site_guild_ids:
            return
        try:
            await self.bot.api.report_discord_event(event.guild_id, event_state(event))
        except NotFoundError:
            self._not_site_guild_ids.add(event.guild_id)
        except APIError:
            logger.warning("Failed to report scheduled event %s in guild %s", event.id, event.guild_id, exc_info=True)

    # --- /eventsync group -----------------------------------------------------------------------------------------

    @eventsync_group.command(name="forum", description="Set (or clear) the forum for event posts.")
    @app_commands.describe(channel="Forum channel for event posts; omit to disable.")
    async def eventsync_forum(
        self, interaction: discord.Interaction[SideQuestBot], channel: discord.ForumChannel | None = None
    ) -> None:
        """Update ``events_forum_channel_id``."""
        if interaction.guild is None:
            return
        await interaction.client.configs.update(
            interaction.guild.id, GuildConfigUpdate(events_forum_channel_id=channel.id if channel else None)
        )
        message = f"Event posts will go to {channel.mention}." if channel else "Event forum posts disabled."
        await interaction.response.send_message(message, ephemeral=True)


async def setup(bot: SideQuestBot) -> None:
    """Register the cog."""
    await bot.add_cog(EventSyncCog(bot))
