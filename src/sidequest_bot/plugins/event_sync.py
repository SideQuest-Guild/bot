"""Two-way guild event <-> Discord scheduled event sync, Discord "Interested" reporting, and the events forum.

Every 15 s: reconcile new guilds once, then pull pending pushes/deletions/activity from the API and apply them to
Discord, acking each. Discord-side listeners report creates/edits/deletes back to the API immediately, and
debounced ``on_scheduled_event_user_add``/``_remove`` listeners report Discord "Interested" clicks. See
``local://event-forum-contract.md`` (read at implementation time) for the full wire contract; this module is the
bot half of it. The API decides what Discord should show; this cog is the only thing that talks to Discord about it.

When a forum channel is configured (``/eventsync forum``), every upcoming push also gets a forum post: one thread
per guild event, tagged by kind, edited in place as the event changes, archived+locked on cancel/complete/deletion,
and commented on as members sign up, go tentative, decline, or withdraw. The starter post also carries persistent
Going/Maybe/Can't buttons (``EventSignupButton``) and a live counts line, so members can sign up from Discord and
see it reflected on the site, and vice versa, within one sync cycle.
"""

import asyncio
import contextlib
import logging
import re
from datetime import UTC, datetime
from functools import lru_cache
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, Final, Self, cast
from uuid import UUID

import discord
from discord import app_commands
from discord.ext import commands, tasks

from sidequest_bot.api_client import APIError, NotFoundError
from sidequest_bot.bot import SideQuestBot  # noqa: TC001  # discord.py evaluates app-command annotations at runtime
from sidequest_common.schemas import (
    EVENT_KIND_LABELS,
    ROLE_KINDS,
    DiscordEventPush,
    DiscordEventReconcile,
    DiscordEventState,
    DiscordEventStatus,
    DiscordInterestReport,
    DiscordPushAck,
    EventKind,
    GuildConfigUpdate,
    Signup,
    SignupActivity,
    SignupRole,
    SignupStatus,
    SignupUpsert,
    WowClass,
)
from sidequest_common.settings import WebSettings

if TYPE_CHECKING:
    from collections.abc import Coroutine, Sequence

logger = logging.getLogger(__name__)

SYNC_INTERVAL_SECONDS = 15
INTEREST_DEBOUNCE_SECONDS = 5
MAX_FORUM_STARTER_LENGTH = 2000
FORUM_THREAD_UPGRADE_SLEEP_SECONDS = 0.5
"""Pause between forum starter edits in ``_upgrade_existing_forum_threads``, to stay rate-limit friendly."""

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

_UUID_PATTERN = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_SIGNUP_CUSTOM_ID_RE: Final = re.compile(rf"^sq:ev:(?P<event_id>{_UUID_PATTERN}):(?P<status>signed|tentative|absent)$")
_CHANGE_CUSTOM_ID_RE: Final = re.compile(rf"^sq:evc:(?P<event_id>{_UUID_PATTERN}):(?P<status>signed|tentative|absent)$")
# Matches the "Sign up: {signup_url}" link in a forum starter post's content (see ``build_starter_message``),
# which ends in "/events/detail/?id={event_id}"; used to recover an existing thread's event id without a push.
_SIGNUP_LINK_ID_RE: Final = re.compile(rf"/events/detail/\?id=({_UUID_PATTERN})")

_SIGNUP_BUTTON_LABEL: dict[SignupStatus, str] = {
    SignupStatus.SIGNED: "Going",
    SignupStatus.TENTATIVE: "Maybe",
    SignupStatus.ABSENT: "Can't",
}
_SIGNUP_BUTTON_STYLE: dict[SignupStatus, discord.ButtonStyle] = {
    SignupStatus.SIGNED: discord.ButtonStyle.success,
    SignupStatus.TENTATIVE: discord.ButtonStyle.secondary,
    SignupStatus.ABSENT: discord.ButtonStyle.danger,
}
_SIGNUP_STATUS_VERB: dict[SignupStatus, str] = {
    SignupStatus.SIGNED: "Going",
    SignupStatus.TENTATIVE: "Maybe",
    SignupStatus.ABSENT: "Can't make it",
}
_ROLE_LABELS: dict[SignupRole, str] = {SignupRole.TANK: "Tank", SignupRole.HEALER: "Healer", SignupRole.DPS: "DPS"}
_CLASS_OPTIONS: Final = [
    discord.SelectOption(label=wow_class.value.capitalize(), value=wow_class.value) for wow_class in WowClass
]
_ROLE_OPTIONS: Final = [discord.SelectOption(label=label, value=role.value) for role, label in _ROLE_LABELS.items()]

_SIGN_IN_MESSAGE = "Sign in at {url} first."
_CLOSED_SIGNUP_ERROR = "Sign-ups for this event are closed."
_GENERIC_SIGNUP_ERROR = "Couldn't record your sign-up; try again shortly."


@lru_cache(maxsize=1)
def _web_settings() -> WebSettings:
    """``WebSettings`` (``WEB_*`` env vars), shared with the API: ``BASE_URL`` for the sign-in link."""
    return WebSettings()


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


def build_counts_line(push: DiscordEventPush) -> str:
    """Live headcounts for the forum starter post, e.g. "Going 12 (2 tanks · 3 healers · 7 DPS) · Maybe 1 · Can't 2".

    Role-kind events (raid, dungeon) break "Going" down by tank/healer/DPS; other kinds just show the total.
    """
    counts = push.counts
    going = f"Going {counts.signed}"
    if push.kind in ROLE_KINDS:
        going += f" ({counts.tank} tanks · {counts.healer} healers · {counts.dps} DPS)"
    return f"{going} · Maybe {counts.tentative} · Can't {counts.absent}"


def build_starter_message(push: DiscordEventPush, *, guild_id: int, discord_event_id: int) -> str:
    """Forum starter-post content for ``push``, trimmed to fit Discord's 2000-char message limit.

    Layout: kind · location, the start (and end) time as Discord timestamps, the live counts line, the event's
    notes (trimmed to fit), the sign-up link, and a link back to the Discord event.
    """
    header_parts = [EVENT_KIND_LABELS[push.kind]]
    if push.location:
        header_parts.append(push.location)
    header = " · ".join(header_parts)
    time_line = f"<t:{int(push.starts_at.timestamp())}:F>"
    time_line += f" - <t:{int(push.ends_at.timestamp())}:F>"
    counts_line = build_counts_line(push)
    links = "\n".join(
        [f"Sign up: {push.signup_url}", f"Discord event: https://discord.com/events/{guild_id}/{discord_event_id}"]
    )
    skeleton = f"{header}\n\n{time_line}\n{counts_line}\n\n{links}"
    notes = _strip_description_footer(push.description)
    if not notes:
        return skeleton
    budget = MAX_FORUM_STARTER_LENGTH - len(skeleton) - 2
    trimmed_notes = _truncate(notes, budget) if budget > 0 else ""
    if not trimmed_notes:
        return skeleton
    return f"{header}\n\n{time_line}\n{counts_line}\n\n{trimmed_notes}\n\n{links}"


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


def extract_event_id_from_starter(content: str) -> UUID | None:
    """The event id embedded in a forum starter post's "Sign up:" link, or ``None`` if it can't be found/parsed."""
    match = _SIGNUP_LINK_ID_RE.search(content)
    if match is None:
        return None
    try:
        return UUID(match.group(1))
    except ValueError:
        return None


# --- Discord sign-up buttons (Going/Maybe/Can't) -----------------------------------------------------------------


def build_signup_custom_id(event_id: UUID, status: SignupStatus) -> str:
    """The ``custom_id`` for one event's Going/Maybe/Can't button."""
    return f"sq:ev:{event_id}:{status.value}"


def parse_signup_custom_id(custom_id: str) -> tuple[UUID, SignupStatus] | None:
    """``(event_id, status)`` if ``custom_id`` matches our template, else ``None``."""
    match = _SIGNUP_CUSTOM_ID_RE.match(custom_id)
    if match is None:
        return None
    return UUID(match["event_id"]), SignupStatus(match["status"])


def build_change_custom_id(event_id: UUID, status: SignupStatus) -> str:
    """The ``custom_id`` for one sign-up's "Change character or role" button."""
    return f"sq:evc:{event_id}:{status.value}"


def parse_change_custom_id(custom_id: str) -> tuple[UUID, SignupStatus] | None:
    """``(event_id, status)`` if ``custom_id`` matches our "change details" template, else ``None``."""
    match = _CHANGE_CUSTOM_ID_RE.match(custom_id)
    if match is None:
        return None
    return UUID(match["event_id"]), SignupStatus(match["status"])


def build_signup_view(event_id: UUID) -> discord.ui.View:
    """A persistent view with the Going/Maybe/Can't buttons for one event's forum starter post."""
    view = discord.ui.View(timeout=None)
    for status in (SignupStatus.SIGNED, SignupStatus.TENTATIVE, SignupStatus.ABSENT):
        view.add_item(EventSignupButton(event_id, status))
    return view


def _guess_class_from_roles(member: discord.Member) -> WowClass | None:
    """The ``WowClass`` matching one of ``member``'s Discord role names (e.g. a ``Warrior`` role), if any."""
    names = {role.name for role in member.roles}
    return next((wow_class for wow_class in WowClass if wow_class.value.capitalize() in names), None)


def _confirmation_text(status: SignupStatus, signup: Signup) -> str:
    """A confirmation line, e.g. "Going as Oakenshield · Warrior · Tank" for a role-kind sign-up.

    The bare form ("Going."/"Maybe."/"Can't make it.") is used for non-role-kind events and "can't make it".
    """
    verb = _SIGNUP_STATUS_VERB[status]
    if signup.character and signup.wow_class and signup.role:
        return f"{verb} as {signup.character} · {signup.wow_class.value.capitalize()} · {_ROLE_LABELS[signup.role]}"
    return f"{verb}."


async def _send_sign_in_prompt(interaction: discord.Interaction[SideQuestBot]) -> None:
    """The clicker has no site account: tell them to sign in, with a link button.

    Always called after the interaction has been deferred (every caller defers before its first API call), so
    this replies through the followup rather than the original response.
    """
    url = f"{_web_settings().BASE_URL.rstrip('/')}/account/"
    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="Sign in", url=url, style=discord.ButtonStyle.link))
    await interaction.followup.send(_SIGN_IN_MESSAGE.format(url=url), view=view, ephemeral=True)


async def _submit_signup(
    interaction: discord.Interaction[SideQuestBot],
    *,
    event_id: UUID,
    status: SignupStatus,
    character: str | None = None,
    wow_class: WowClass | None = None,
    role: SignupRole | None = None,
) -> None:
    """Upsert the signup via the API and reply ephemerally through the followup.

    Always called after the interaction has been deferred: from ``_handle_signup_click`` (deferred before its
    lookup) and from :meth:`SignupDetailsModal.on_submit` (deferred before this call).
    """
    guild = interaction.guild
    if guild is None:
        return
    data = SignupUpsert(status=status, character=character, wow_class=wow_class, role=role)
    try:
        signup = await interaction.client.api.upsert_event_signup_by_discord(
            guild.id, event_id, interaction.user.id, data
        )
    except NotFoundError:
        await _send_sign_in_prompt(interaction)
        return
    except APIError as exc:
        message = _CLOSED_SIGNUP_ERROR if exc.status_code == HTTPStatus.CONFLICT else _GENERIC_SIGNUP_ERROR
        await interaction.followup.send(message, ephemeral=True)
        return
    content = _confirmation_text(status, signup)
    if role is None:
        await interaction.followup.send(content, ephemeral=True)
        return
    change_view = discord.ui.View(timeout=300)
    change_view.add_item(ChangeSignupDetailsButton(event_id, status))
    await interaction.followup.send(content, view=change_view, ephemeral=True)


class _EnterDetailsView(discord.ui.View):
    """Ephemeral view with one button that opens the signup details modal, prefilled from closure state.

    Not a :class:`discord.ui.DynamicItem`: this button only ever lives inside one interaction's followup message
    (timeout bound, not persisted across restarts), so it doesn't need ``custom_id`` reconstruction. Opening the
    modal from its callback makes no API call, so it's always well within Discord's 3-second response window.
    """

    def __init__(
        self,
        *,
        event_id: UUID,
        status: SignupStatus,
        character: str | None = None,
        wow_class: WowClass | None = None,
        role: SignupRole | None = None,
    ) -> None:
        super().__init__(timeout=300)
        self.event_id = event_id
        self.status = status
        self.character = character
        self.wow_class = wow_class
        self.role = role

    @discord.ui.button(label="Enter character and role", style=discord.ButtonStyle.primary)
    async def enter_details(
        self, interaction: discord.Interaction[SideQuestBot], _button: discord.ui.Button[Self]
    ) -> None:
        """Open the details modal immediately: no network call, so it's safe within Discord's 3s window."""
        modal = SignupDetailsModal(
            event_id=self.event_id,
            status=self.status,
            character=self.character,
            wow_class=self.wow_class,
            role=self.role,
        )
        await interaction.response.send_modal(modal)


async def _handle_signup_click(
    interaction: discord.Interaction[SideQuestBot], *, event_id: UUID, status: SignupStatus
) -> None:
    """Route a Going/Maybe/Can't click: sign-in prompt, bare RSVP, auto-fill from past details, or a details modal.

    Defers (ephemeral, "thinking") before the lookup GET, the only way to stay within Discord's 3-second ack
    window once a network call is involved. A role-kind event with no known details can't send a modal at this
    point (Discord doesn't allow a modal after a deferral), so it instead offers a button that opens the modal
    on a fresh interaction, prefilled only from the member's Discord class role (no further API call needed).
    """
    guild = interaction.guild
    if guild is None or not isinstance(interaction.user, discord.Member):
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        lookup = await interaction.client.api.get_event_signup_by_discord(guild.id, event_id, interaction.user.id)
    except NotFoundError:
        await _send_sign_in_prompt(interaction)
        return
    except APIError:
        await interaction.followup.send(_GENERIC_SIGNUP_ERROR, ephemeral=True)
        return
    if status == SignupStatus.ABSENT or lookup.kind not in ROLE_KINDS:
        await _submit_signup(interaction, event_id=event_id, status=status)
        return
    defaults = lookup.defaults
    if defaults is not None:
        await _submit_signup(
            interaction,
            event_id=event_id,
            status=status,
            character=defaults.character,
            wow_class=defaults.wow_class,
            role=defaults.role,
        )
        return
    view = _EnterDetailsView(event_id=event_id, status=status, wow_class=_guess_class_from_roles(interaction.user))
    await interaction.followup.send("Enter your character and role to finish signing up.", ephemeral=True, view=view)


class SignupDetailsModal(discord.ui.Modal, title="Sign up for this event"):
    """Character name, class, and role, for a role-kind event's Going/Maybe sign-up (new or changed)."""

    character_label = discord.ui.Label(
        text="Character name", component=discord.ui.TextInput(max_length=24, placeholder="Oakenshield")
    )
    class_label = discord.ui.Label(text="Class", component=discord.ui.Select(options=_CLASS_OPTIONS))
    role_label = discord.ui.Label(text="Role", component=discord.ui.Select(options=_ROLE_OPTIONS))

    @property
    def _character_input(self) -> discord.ui.TextInput[Self]:
        """``character_label.component``, typed: ``Label.component`` is declared as the untyped base ``Item``."""
        return cast("discord.ui.TextInput[Self]", self.character_label.component)

    @property
    def _class_select(self) -> discord.ui.Select[Self]:
        """``class_label.component``, typed."""
        return cast("discord.ui.Select[Self]", self.class_label.component)

    @property
    def _role_select(self) -> discord.ui.Select[Self]:
        """``role_label.component``, typed."""
        return cast("discord.ui.Select[Self]", self.role_label.component)

    def __init__(
        self,
        *,
        event_id: UUID,
        status: SignupStatus,
        character: str | None = None,
        wow_class: WowClass | None = None,
        role: SignupRole | None = None,
    ) -> None:
        super().__init__()
        self.event_id = event_id
        self.status = status
        if character is not None:
            self._character_input.default = character
        if wow_class is not None:
            for option in self._class_select.options:
                option.default = option.value == wow_class.value
        if role is not None:
            for option in self._role_select.options:
                option.default = option.value == role.value

    async def on_submit(self, interaction: discord.Interaction[SideQuestBot]) -> None:
        """Defer, then submit the chosen character/class/role as the pending sign-up and reply via followup."""
        character = self._character_input.value
        wow_class = WowClass(self._class_select.values[0])
        role = SignupRole(self._role_select.values[0])
        await interaction.response.defer(ephemeral=True, thinking=True)
        await _submit_signup(
            interaction,
            event_id=self.event_id,
            status=self.status,
            character=character,
            wow_class=wow_class,
            role=role,
        )


class EventSignupButton(discord.ui.DynamicItem[discord.ui.Button], template=_SIGNUP_CUSTOM_ID_RE):
    """A Going/Maybe/Can't button on an event's forum starter post; state lives entirely in its ``custom_id``."""

    def __init__(self, event_id: UUID, status: SignupStatus) -> None:
        super().__init__(
            discord.ui.Button(
                style=_SIGNUP_BUTTON_STYLE[status],
                label=_SIGNUP_BUTTON_LABEL[status],
                custom_id=build_signup_custom_id(event_id, status),
            )
        )
        self.event_id = event_id
        self.status = status

    @classmethod
    async def from_custom_id(
        cls, _interaction: discord.Interaction[Any], _item: discord.ui.Item[Any], match: re.Match[str], /
    ) -> Self:
        """Rebuild the button from a dispatched component after a restart."""
        return cls(UUID(match["event_id"]), SignupStatus(match["status"]))

    async def callback(self, interaction: discord.Interaction[SideQuestBot]) -> None:
        """Route the click through ``_handle_signup_click``."""
        await _handle_signup_click(interaction, event_id=self.event_id, status=self.status)


class ChangeSignupDetailsButton(discord.ui.DynamicItem[discord.ui.Button], template=_CHANGE_CUSTOM_ID_RE):
    """The "Change character or role" button on a sign-up confirmation; opens the details modal pre-filled."""

    def __init__(self, event_id: UUID, status: SignupStatus) -> None:
        super().__init__(
            discord.ui.Button(
                style=discord.ButtonStyle.secondary,
                label="Change character or role",
                custom_id=build_change_custom_id(event_id, status),
            )
        )
        self.event_id = event_id
        self.status = status

    @classmethod
    async def from_custom_id(
        cls, _interaction: discord.Interaction[Any], _item: discord.ui.Item[Any], match: re.Match[str], /
    ) -> Self:
        """Rebuild the button from a dispatched component after a restart."""
        return cls(UUID(match["event_id"]), SignupStatus(match["status"]))

    async def callback(self, interaction: discord.Interaction[SideQuestBot]) -> None:
        """Re-fetch the member's current signup, then offer a button that opens the details modal pre-filled.

        Defers before the lookup GET (same reasoning as ``_handle_signup_click``) and so can't send the modal
        directly from this callback; the followup's button opens it on a fresh interaction instead.
        """
        guild = interaction.guild
        if guild is None or not isinstance(interaction.user, discord.Member):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            lookup = await interaction.client.api.get_event_signup_by_discord(
                guild.id, self.event_id, interaction.user.id
            )
        except NotFoundError:
            await _send_sign_in_prompt(interaction)
            return
        except APIError:
            await interaction.followup.send(_GENERIC_SIGNUP_ERROR, ephemeral=True)
            return
        current = lookup.signup
        view = _EnterDetailsView(
            event_id=self.event_id,
            status=self.status,
            character=current.character if current else None,
            wow_class=current.wow_class if current else None,
            role=current.role if current else None,
        )
        await interaction.followup.send(
            "Enter your character and role to update your sign-up.", ephemeral=True, view=view
        )


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
        await self._upgrade_existing_forum_threads(guild)

    async def _upgrade_existing_forum_threads(self, guild: discord.Guild) -> None:
        """One-time per-guild catch-up: give every active forum thread missing sign-up buttons its view.

        An old worker mid rolling-deploy can edit a forum starter post without buttons (it doesn't know the
        buttons exist) and still ack the push's content hash, so a new worker's ``list_pending`` never flags
        that event again: the hash only tracks content, not whether the components landed. Walk every active
        forum thread here instead of trusting that hash, so a missed button upgrade still lands on the next
        reconcile. Sequential with a small sleep between edits to stay rate-limit friendly.
        """
        config = await self.bot.configs.get(guild.id)
        if config.events_forum_channel_id is None:
            return
        forum = guild.get_channel(config.events_forum_channel_id)
        if not isinstance(forum, discord.ForumChannel):
            return
        for thread in forum.threads:
            await self._upgrade_one_forum_thread(thread)
            await asyncio.sleep(FORUM_THREAD_UPGRADE_SLEEP_SECONDS)

    async def _upgrade_one_forum_thread(self, thread: discord.Thread) -> None:
        """Add the sign-up view to ``thread``'s starter post if it's missing one and we can recover its event id."""
        try:
            starter = await thread.fetch_message(thread.id)
        except discord.NotFound, discord.Forbidden:
            return
        if starter.components:
            return
        event_id = extract_event_id_from_starter(starter.content)
        if event_id is None:
            return
        try:
            await starter.edit(view=build_signup_view(event_id))
        except discord.HTTPException:
            logger.warning(
                "Failed to upgrade forum starter buttons for thread %s in guild %s",
                thread.id,
                thread.guild.id,
                exc_info=True,
            )

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
        view = build_signup_view(push.event_id)
        if push.thread_id is None:
            tag = await self._ensure_tag(forum, push.kind)
            try:
                created = await forum.create_thread(
                    name=name, content=content, applied_tags=[tag] if tag else [], view=view
                )
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
            await starter.edit(content=content, view=view)
        except discord.Forbidden:
            self._warn_forum_forbidden(forum.guild.id)
        except discord.NotFound:
            pass
        return push.thread_id

    async def _clear_starter_buttons(self, thread: discord.Thread) -> None:
        """Remove the Going/Maybe/Can't buttons from ``thread``'s starter message (cancelled/completed/deleted)."""
        starter = await thread.fetch_message(thread.id)
        await starter.edit(view=None)

    async def _ensure_cancel_or_complete_thread(
        self, forum: discord.ForumChannel, push: DiscordEventPush
    ) -> int | None:
        if push.thread_id is None:
            return None
        thread = await self._fetch_thread(forum.guild, push.thread_id)
        if thread is None:
            return None
        try:
            await self._clear_starter_buttons(thread)
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
            await self._clear_starter_buttons(thread)
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
    """Register the persistent sign-up buttons and the cog."""
    bot.add_dynamic_items(EventSignupButton, ChangeSignupDetailsButton)
    await bot.add_cog(EventSyncCog(bot))
