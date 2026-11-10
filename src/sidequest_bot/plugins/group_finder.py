"""Ephemeral group voice channels ("LFG"): the ``#lfg`` panel, spawn flow, owner controls, and teardown.

A locked ``#lfg`` text channel holds one panel message with one button per :data:`GROUP_KINDS`. Clicking a button
creates a voice channel right away (``dungeon-1``, ``raid-2``, ...) in the panel's category, with an ephemeral
"Add details" follow-up to name it, add a note, flag needed roles, invite members, and ping the kind's opt-in role.
A live card in ``#lfg`` tracks each group's headcount; owner controls (Rename, Set cap, Lock/Unlock, Transfer, End)
live as buttons in the channel's own text chat. Every 60s the bot adds a minute to everyone present (and to every
pair present together, for "who groups with whom" stats) and pushes the running totals to the API every five
minutes and at close. A channel empty (or never joined) for :data:`EMPTY_GRACE_SECONDS` is torn down: its chat is
captured as a transcript, the session is closed with the API, its card is deleted, the channel is deleted, and a
one-line recap posts to the guild log channel. The API is the only source of truth for anything that must survive a
restart; ``GET /groups/open`` on reconnect re-adopts channels that are still live and closes (reason ``lost``) any
the bot can no longer find.

An event thread or weekly-board card can also carry an **Open voice** button (``OpenVoiceButton``), which spawns a
group linked to that scheduled event instead of a fresh numbered one; while that group is live, ``attendance_voice``
counts the event's voice minutes only from its channel.

The panel's **PUG group** button (only usable once an officer sets ``GuildConfig.pug_role_id`` with ``/lfg
pugrole``) spawns a ``pug-<kind>-<n>`` channel plus a temporary per-group role and an invite link whose Community
Invites ``role_ids`` grant both the guild PUG role and the group role on join, so outside players land seeing only
their group and #sign-the-ledger. A 60s sweep (``_pug_sweep``) lifts the guild PUG role once a joiner signs in or
is confirmed, and kicks one who's held no live group's PUG role for 30 minutes — but only if every other role
they hold is the guild PUG role, the gate role, or a group PUG role, so a joiner who picked up any other role is
never kicked. Teardown deletes a PUG group's invite and role once the channel closes.

Pure logic (name allocation, slugs, presence accumulation, teardown timing, owner succession, card/recap text) is
unit tested directly; Discord I/O is exercised only through the plugin's effects, not mocked by hand.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import math
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib.resources import files
from typing import TYPE_CHECKING, Any, Literal, Self, cast
from uuid import UUID

import discord
from discord import app_commands
from discord.ext import commands, tasks
from discord.http import Route

from sidequest_bot.api_client import APIError, NotFoundError
from sidequest_bot.bot import SideQuestBot  # noqa: TC001  # discord.py evaluates app-command annotations at runtime
from sidequest_bot.lib.embeds import Colors
from sidequest_bot.plugins.welcome import autorole_block_reason
from sidequest_common.schemas import (
    EMPTY_GRACE_SECONDS,
    GROUP_KIND_DEFAULT_CAP,
    GROUP_KIND_LABELS,
    GROUP_KIND_PREFIX,
    GROUP_KINDS,
    MAX_GROUP_CAP,
    MAX_GROUP_NAME_LENGTH,
    MAX_GROUP_NOTE_LENGTH,
    MAX_LIVE_GROUPS,
    MAX_TRANSCRIPT_MESSAGES,
    EventKind,
    GroupEndReason,
    GroupMemberMinutes,
    GroupPairMinutes,
    GroupPresenceReport,
    GroupSessionClose,
    GroupSessionCreate,
    GroupSessionSchema,
    GroupSessionUpdate,
    GroupTranscriptMessage,
    GuildConfigUpdate,
    SignupRole,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from sidequest_common.schemas import EventSummary, GuildConfigSchema

logger = logging.getLogger(__name__)

PRESENCE_TICK_SECONDS = 60
"""One minute per tick, matching :data:`COUNTED_GROUP_MINUTES` and the per-member minute counters."""
PRESENCE_PUSH_INTERVAL_SECONDS = 300
TEARDOWN_CHECK_SECONDS = 15
CARD_DEBOUNCE_SECONDS = 5
DAILY_PURGE_INTERVAL_HOURS = 24

RENAME_WINDOW_SECONDS = 600
RENAME_LIMIT = 2
"""Discord allows about two channel renames per ten minutes."""

MAX_CHANNEL_NAME_LENGTH = 100
MIN_GROUPED_MEMBERS = 2
"""Minimum non-bot members present for a minute to count toward ``grouped_minutes``."""
MINUTES_PER_HOUR = 60

PUG_PREFIX = "pug"
PUG_SWEEP_INTERVAL_SECONDS = 60
PUG_ORPHAN_KICK_SECONDS = 1800
"""How long a PUG joiner may hold no live group's PUG role before being kicked for never signing the ledger."""
PUG_INVITE_MAX_AGE_SECONDS = 86400
PUG_ROLE_NAME_PREFIX = "PUG · "

NEED_LABELS: dict[SignupRole, str] = {SignupRole.TANK: "Tank", SignupRole.HEALER: "Healer", SignupRole.DPS: "DPS"}
NEED_EMOJI: dict[SignupRole, str] = {SignupRole.TANK: "🛡️", SignupRole.HEALER: "💚", SignupRole.DPS: "🗡️"}
_NEED_OPTIONS = [
    discord.CheckboxGroupOption(label=f"{NEED_EMOJI[role]} {label}", value=role.value)
    for role, label in NEED_LABELS.items()
]

KIND_EMOJI: dict[EventKind, str] = {
    EventKind.DUNGEON: "🗝️",
    EventKind.RAID: "🐉",
    EventKind.WORLD_BOSS: "👹",
    EventKind.LEVELING: "🧭",
    EventKind.PVP: "⚔️",
    EventKind.SOCIAL: "🍻",
}
KIND_BLURBS: dict[EventKind, str] = {
    EventKind.DUNGEON: "Runs, carries and attunements",
    EventKind.RAID: "Raid nights and pugs; set a 10 or 20 cap after",
    EventKind.WORLD_BOSS: "Rally on a world boss or world event",
    EventKind.LEVELING: "Questing and grinding together",
    EventKind.PVP: "Battlegrounds and world PvP",
    EventKind.SOCIAL: "Hang out in voice, no plan needed",
}
KIND_COLORS: dict[EventKind, discord.Colour] = {
    EventKind.DUNGEON: discord.Colour(0x4A9DF2),
    EventKind.RAID: discord.Colour(0xB673F5),
    EventKind.WORLD_BOSS: discord.Colour(0xFF8C2A),
    EventKind.LEVELING: discord.Colour(0x5FD38D),
    EventKind.PVP: discord.Colour(0xE0533D),
    EventKind.SOCIAL: Colors.SITE_GOLD,
}
"""Card accent per kind, after WoW's item-quality colors so a glance tells the kinds apart."""

BANNER_FILENAME = "lfg-banner.jpg"
MAX_CARD_MEMBERS_SHOWN = 10
MAX_CAP_PIPS = 10
"""Groups capped at this many or fewer get a filled/empty slot bar on their card."""
_BANNER = files("sidequest_bot") / "assets" / "lfg" / "banner.jpg"

_UUID_PATTERN = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_SPAWN_CUSTOM_ID_RE = re.compile(r"^sq:lfg:spawn:(?P<kind>[a-z_]+)$")
_OWNER_CUSTOM_ID_RE = re.compile(
    rf"^sq:lfg:owner:(?P<action>rename|cap|locktoggle|transfer|end):(?P<session_id>{_UUID_PATTERN})$"
)
_EVENT_CUSTOM_ID_RE = re.compile(rf"^sq:lfg:event:(?P<event_id>{_UUID_PATTERN})$")
_PUG_CUSTOM_ID = "sq:lfg:pug"
_PUG_CUSTOM_ID_RE = re.compile(r"^sq:lfg:pug$")

_KIND_CHOICES = [app_commands.Choice(name=GROUP_KIND_LABELS[kind], value=kind.value) for kind in GROUP_KINDS]

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_DIGITS_RE = re.compile(r"^\d+$")


# --- Pure logic (unit tested directly) -----------------------------------------------------------------------


def allocate_group_number(taken: Iterable[int]) -> int:
    """The lowest number, starting at 1, not already in ``taken``."""
    taken_set = set(taken)
    number = 1
    while number in taken_set:
        number += 1
    return number


def extract_taken_numbers(names: Iterable[str], prefix: str) -> set[int]:
    """Numbers already used by default-named channels (``<prefix>-<N>``) among ``names``."""
    taken: set[int] = set()
    for name in names:
        if not name.startswith(f"{prefix}-"):
            continue
        rest = name[len(prefix) + 1 :]
        if _DIGITS_RE.match(rest):
            taken.add(int(rest))
    return taken


def default_channel_name(kind: EventKind, number: int) -> str:
    """``dungeon-1``, ``raid-2``, and so on."""
    return f"{GROUP_KIND_PREFIX[kind]}-{number}"


def slugify(text: str, *, max_length: int = 40) -> str:
    """Lowercase, ASCII, hyphen-separated slug of ``text``; empty if nothing alphanumeric survives."""
    normalized = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    slug = _SLUG_RE.sub("-", normalized.lower()).strip("-")
    return slug[:max_length].strip("-")


def named_channel_name(kind: EventKind, slug: str, existing: set[str]) -> str:
    """``<prefix>-<slug>``, deduplicated against ``existing`` channel names with a ``-2``, ``-3``, ... suffix."""
    prefix = GROUP_KIND_PREFIX[kind]
    base = f"{prefix}-{slug}" if slug else prefix
    base = base[:MAX_CHANNEL_NAME_LENGTH]
    if base not in existing:
        return base
    suffix = 2
    while True:
        tail = f"-{suffix}"
        candidate = f"{base[: MAX_CHANNEL_NAME_LENGTH - len(tail)]}{tail}"
        if candidate not in existing:
            return candidate
        suffix += 1


def pug_channel_name(kind: EventKind, number: int) -> str:
    """``pug-raid-1``, and so on: marks a PUG group's channel distinctly from a regular one of the same kind."""
    return f"{PUG_PREFIX}-{default_channel_name(kind, number)}"


def pair_key(a: int, b: int) -> tuple[int, int]:
    """``(a, b)`` with the smaller id first, matching ``GroupSessionPair.discord_id_a < discord_id_b``."""
    return (a, b) if a < b else (b, a)


def tick_presence(
    *,
    present_ids: Iterable[int],
    minutes: Mapping[int, int],
    pairs: Mapping[tuple[int, int], int],
    peak_size: int,
    grouped_minutes: int,
) -> tuple[dict[int, int], dict[tuple[int, int], int], int, int]:
    """One minute's presence tick: every present member +1 minute, every present pair +1 minute together.

    ``grouped_minutes`` only rises while two or more non-bot members are present at once.
    """
    ids = sorted(set(present_ids))
    new_minutes = dict(minutes)
    for member_id in ids:
        new_minutes[member_id] = new_minutes.get(member_id, 0) + 1
    new_pairs = dict(pairs)
    for index, a in enumerate(ids):
        for b in ids[index + 1 :]:
            key = pair_key(a, b)
            new_pairs[key] = new_pairs.get(key, 0) + 1
    new_peak = max(peak_size, len(ids))
    new_grouped = grouped_minutes + (1 if len(ids) >= MIN_GROUPED_MEMBERS else 0)
    return new_minutes, new_pairs, new_peak, new_grouped


def should_tear_down(empty_since: datetime | None, now: datetime) -> bool:
    """A channel empty (or never joined) for :data:`EMPTY_GRACE_SECONDS` or more should be torn down."""
    return empty_since is not None and (now - empty_since).total_seconds() >= EMPTY_GRACE_SECONDS


def _has_people(channel: object) -> bool:
    """Whether ``channel`` is a voice channel with any non-bot member in it right now."""
    return isinstance(channel, discord.VoiceChannel) and any(not member.bot for member in channel.members)


def next_owner(current_owner_id: int, present_ids: Sequence[int], join_order: Mapping[int, datetime]) -> int | None:
    """The longest-present member other than ``current_owner_id``, or ``None`` if nobody else is present."""
    candidates = [member_id for member_id in present_ids if member_id != current_owner_id]
    if not candidates:
        return None
    fallback = datetime.max.replace(tzinfo=UTC)  # unknown join time: treat as just joined, never preferred
    return min(candidates, key=lambda member_id: join_order.get(member_id, fallback))


def rename_cooldown_minutes(rename_times: Sequence[datetime], now: datetime) -> int:
    """Minutes until another rename is allowed, or 0 if one is allowed now."""
    recent = [t for t in rename_times if (now - t).total_seconds() < RENAME_WINDOW_SECONDS]
    if len(recent) < RENAME_LIMIT:
        return 0
    oldest = min(recent)
    remaining = RENAME_WINDOW_SECONDS - (now - oldest).total_seconds()
    return max(1, math.ceil(remaining / 60))


def pug_sweep_decision(
    *,
    member_role_ids: set[int],
    assignable_role_ids: set[int],
    linked_role_id: int | None,
    roster_role_id: int | None,
    pug_role_id: int,
    gate_role_id: int | None,
    group_pug_role_ids: set[int],
    orphaned_since: datetime | None,
    now: datetime,
) -> Literal["none", "strip", "kick"]:
    """What to do, this tick, about one member holding the guild's PUG role.

    ``"strip"``: they're confirmed (hold ``linked_role_id`` or ``roster_role_id``) — the guild-wide PUG role is no
    longer needed, though any per-group PUG role is left alone so they stay in their group. ``"kick"``: they've held
    no live group's PUG role (``group_pug_role_ids``) for at least :data:`PUG_ORPHAN_KICK_SECONDS` since
    ``orphaned_since``, AND every assignable role they hold besides ``@everyone`` is the guild PUG role, the gate
    role, or a group PUG role — anyone holding any other role is never kicked. ``"none"`` otherwise, including while
    still within a live group or not yet past the orphan window (the caller starts/clears ``orphaned_since``).
    """
    if (linked_role_id is not None and linked_role_id in member_role_ids) or (
        roster_role_id is not None and roster_role_id in member_role_ids
    ):
        return "strip" if pug_role_id in member_role_ids else "none"
    if member_role_ids & group_pug_role_ids:
        return "none"
    if orphaned_since is None or (now - orphaned_since).total_seconds() < PUG_ORPHAN_KICK_SECONDS:
        return "none"
    allowed = {pug_role_id, gate_role_id} | group_pug_role_ids
    if assignable_role_ids - allowed:
        return "none"
    return "kick"


def cap_pips(member_count: int, cap: int | None) -> str:
    """A filled/empty slot bar such as ``▰▰▰▱▱`` for small capped groups; ``""`` when uncapped or over the pip limit."""
    if cap is None or cap > MAX_CAP_PIPS:
        return ""
    filled = min(member_count, cap)
    return "▰" * filled + "▱" * (cap - filled)


@dataclass(frozen=True)
class CardContent:
    """A live LFG card's text: ``header`` sits beside the Join button, ``details`` below it, ``footer`` last."""

    header: str
    details: str
    footer: str
    full: bool


def build_card_content(
    *,
    kind: EventKind,
    display_name: str,
    member_ids: Sequence[int],
    cap: int | None,
    needs: Sequence[SignupRole],
    note: str | None,
    owner_id: int,
    locked: bool,
    started_at: datetime,
) -> CardContent:
    """The live card for one group: who's in it against its cap, what it needs, its note, and who leads it."""
    count = len(member_ids)
    if count == 0:
        status = "Waiting for the first player"
    elif cap:
        status = f"**{count}/{cap}** in voice"
    else:
        status = f"**{count}** in voice"
    pips = cap_pips(count, cap)
    header_lines = [f"### {KIND_EMOJI[kind]} {display_name}", f"{status}{f'  {pips}' if pips else ''}"]
    detail_lines = [f"👑 Led by <@{owner_id}>" + (" · 🔒 Invite only" if locked else "")]
    if needs:
        detail_lines.append("**Needs** " + " · ".join(f"{NEED_EMOJI[role]} {NEED_LABELS[role]}" for role in needs))
    if note:
        detail_lines.append(f"> {note}")
    if member_ids:
        shown = " ".join(f"<@{member_id}>" for member_id in member_ids[:MAX_CARD_MEMBERS_SHOWN])
        hidden = count - MAX_CARD_MEMBERS_SHOWN
        detail_lines.append(f"**In voice** {shown}" + (f" +{hidden} more" if hidden > 0 else ""))
    footer = f"-# {GROUP_KIND_LABELS[kind]} · started {discord.utils.format_dt(started_at, style='R')}"
    return CardContent(
        header="\n".join(header_lines),
        details="\n".join(detail_lines),
        footer=footer,
        full=cap is not None and count >= cap,
    )


def format_duration(minutes: int) -> str:
    """``45m``, ``1h30m``, or ``2h``."""
    if minutes < MINUTES_PER_HOUR:
        return f"{minutes}m"
    hours, rest = divmod(minutes, MINUTES_PER_HOUR)
    return f"{hours}h{rest}m" if rest else f"{hours}h"


def build_recap_line(*, display_name: str, duration_minutes: int, participant_names: Sequence[str]) -> str:
    """The one-line teardown recap posted to the guild log channel."""
    names = ", ".join(participant_names) if participant_names else "nobody"
    return f"🗡 {display_name} · {format_duration(duration_minutes)} · {names}"


def mapped_group_kind(kind: EventKind) -> EventKind:
    """``kind`` if it's one of :data:`GROUP_KINDS`, else social (the "Open voice" button's fallback)."""
    return kind if kind in GROUP_KINDS else EventKind.SOCIAL


def build_open_voice_custom_id(event_id: UUID) -> str:
    """The ``custom_id`` for an event's "Open voice" button."""
    return f"sq:lfg:event:{event_id}"


def can_spawn(member: discord.Member, roster_role_id: int | None) -> bool:
    """Roster-confirmed members, or officers with Manage Channels, may spawn a group."""
    if member.guild_permissions.manage_channels:
        return True
    return roster_role_id is not None and any(role.id == roster_role_id for role in member.roles)


def build_presence_report(live: LiveGroup) -> GroupPresenceReport:
    """``live``'s current cumulative presence, shaped for ``PUT .../presence`` or a close."""
    members = [
        GroupMemberMinutes(
            discord_id=str(member_id), display_name=live.display_names.get(member_id, str(member_id)), minutes=minutes
        )
        for member_id, minutes in live.minutes.items()
    ]
    pairs = [
        GroupPairMinutes(discord_id_a=str(a), discord_id_b=str(b), minutes=minutes)
        for (a, b), minutes in live.pairs.items()
    ]
    return GroupPresenceReport(
        peak_size=live.peak_size, grouped_minutes=live.grouped_minutes, members=members, pairs=pairs
    )


@dataclass
class LiveGroup:
    """In-memory state for one live group voice channel, guarded by its own lock against concurrent events."""

    session_id: UUID
    guild_id: int
    channel_id: int
    kind: EventKind
    creator_id: int
    owner_id: int
    channel_name: str
    name: str | None
    note: str | None
    needs: list[SignupRole]
    cap: int | None
    event_id: UUID | None
    card_message_id: int | None
    started_at: datetime
    minutes: dict[int, int] = field(default_factory=dict)
    pairs: dict[tuple[int, int], int] = field(default_factory=dict)
    display_names: dict[int, str] = field(default_factory=dict)
    peak_size: int = 0
    grouped_minutes: int = 0
    empty_since: datetime | None = None
    ever_joined: bool = False
    join_order: dict[int, datetime] = field(default_factory=dict)
    invited_ids: set[int] = field(default_factory=set)
    locked: bool = False
    rename_times: list[datetime] = field(default_factory=list)
    last_push_at: datetime | None = None
    last_card: CardContent | None = None
    """The card last drawn, so an unchanged card isn't edited again."""
    pug_role_id: int | None = None
    """Set for a PUG group: its temporary per-group Discord role."""
    invite_code: str | None = None
    """Set for a PUG group whose invite is still live. Not persisted: lost (and not recreated) across a restart."""
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False, compare=False)

    @property
    def display_name(self) -> str:
        """``name`` once set (e.g. by Add details), else the channel's current name."""
        return self.name or self.channel_name

    @property
    def invite_url(self) -> str | None:
        """The PUG group's current invite link, or ``None`` if it isn't a PUG group or the invite is gone."""
        return f"https://discord.gg/{self.invite_code}" if self.invite_code else None


# --- Spawn panel ----------------------------------------------------------------------------------------------


def _cap_text(kind: EventKind) -> str:
    cap = GROUP_KIND_DEFAULT_CAP[kind]
    return f"{cap} players" if cap else "No cap"


def banner_file() -> discord.File:
    """The panel's banner painting (adventurers mustering at the gate), attached to the panel message."""
    return discord.File(io.BytesIO(_BANNER.read_bytes()), filename=BANNER_FILENAME, description="Adventurers mustering")


def build_panel_layout() -> discord.ui.LayoutView:
    """The ``#lfg`` panel: banner, a one-line pitch, one row per kind with its Start button, and how it works."""
    container = discord.ui.Container(accent_colour=Colors.SITE_GOLD)
    container.add_item(
        discord.ui.MediaGallery(
            discord.MediaGalleryItem(f"attachment://{BANNER_FILENAME}", description="Adventurers mustering at the gate")
        )
    )
    container.add_item(
        discord.ui.TextDisplay(
            "## Looking for Group\n"
            "Start a voice channel in one click and it's yours. Name it, flag the roles you need and ping friends "
            "after, or just jump in."
        )
    )
    container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.large))
    for kind in GROUP_KINDS:
        container.add_item(
            discord.ui.Section(
                f"**{KIND_EMOJI[kind]} {GROUP_KIND_LABELS[kind]}**\n-# {_cap_text(kind)} · {KIND_BLURBS[kind]}",
                accessory=_in_layout(SpawnButton(kind)),
            )
        )
    container.add_item(
        discord.ui.Section(
            "**🌐 PUG group**\n-# Invite outside players; they see only their group and #sign-the-ledger",
            accessory=_in_layout(PugButton()),
        )
    )
    container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.large))
    container.add_item(
        discord.ui.TextDisplay(
            "-# You get a **Join** link and an **Add details** button. The owner can rename, set a cap, lock, hand off "
            "or end the group from its chat. Empty channels close after 2 minutes; group chat is kept 30 days for "
            "officers."
        )
    )
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    return view


def build_card_layout(content: CardContent, *, kind: EventKind, join_url: str) -> discord.ui.LayoutView:
    """One live group's card in ``#lfg``: kind-colored, with a Join button beside its name (greyed out when full)."""
    join = discord.ui.Button(
        style=discord.ButtonStyle.link, label="Full" if content.full else "Join", url=join_url, disabled=content.full
    )
    container = discord.ui.Container(accent_colour=KIND_COLORS[kind])
    container.add_item(discord.ui.Section(content.header, accessory=join))
    container.add_item(discord.ui.TextDisplay(content.details))
    container.add_item(discord.ui.TextDisplay(content.footer))
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    return view


def build_owner_controls_layout(
    session_id: UUID, *, kind: EventKind, locked: bool, invite_url: str | None = None
) -> discord.ui.LayoutView:
    """The owner controls posted once in a group channel's text chat.

    ``invite_url`` adds a line with the PUG group's invite link, for a PUG group whose invite is still live.
    """
    container = discord.ui.Container(accent_colour=KIND_COLORS[kind])
    container.add_item(
        discord.ui.TextDisplay(
            "### 👑 Group controls\n"
            "For the group's owner and officers. If the owner leaves, whoever has been here longest takes over."
        )
    )
    container.add_item(
        discord.ui.ActionRow(
            *(
                _in_layout(OwnerControlButton(session_id, action, locked=locked))
                for action in ("rename", "cap", "locktoggle", "transfer", "end")
            )
        )
    )
    if invite_url is not None:
        container.add_item(discord.ui.TextDisplay(f"🌐 **Invite link for outside players:** {invite_url}"))
    container.add_item(
        discord.ui.TextDisplay(
            "-# This channel closes 2 minutes after it empties. Chat here is kept 30 days for officers."
        )
    )
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    return view


def _in_layout(item: discord.ui.DynamicItem[Any]) -> discord.ui.Item[discord.ui.LayoutView]:
    """``item`` typed for a layout's ``Section`` or ``ActionRow``.

    discord.py declares every ``DynamicItem`` as ``Item[View | LayoutView]``, which the layout-only containers reject
    even though dispatch works the same in both kinds of view.
    """
    return cast("discord.ui.Item[discord.ui.LayoutView]", item)


class SpawnButton(discord.ui.DynamicItem[discord.ui.Button], template=_SPAWN_CUSTOM_ID_RE):
    """One ``#lfg`` panel button; state (the kind) lives entirely in its ``custom_id``."""

    def __init__(self, kind: EventKind) -> None:
        super().__init__(
            discord.ui.Button(
                style=discord.ButtonStyle.primary,
                label="Start",
                emoji=KIND_EMOJI[kind],
                custom_id=f"sq:lfg:spawn:{kind.value}",
            )
        )
        self.kind = kind

    @classmethod
    async def from_custom_id(
        cls, _interaction: discord.Interaction[SideQuestBot], _item: discord.ui.Item[Any], match: re.Match[str], /
    ) -> Self:
        """Rebuild the button from a dispatched component after a restart."""
        return cls(EventKind(match["kind"]))

    async def callback(self, interaction: discord.Interaction[SideQuestBot]) -> None:
        """Route the click to :meth:`GroupFinderCog.handle_spawn_click`."""
        cog = interaction.client.get_cog("GroupFinderCog")
        if isinstance(cog, GroupFinderCog):
            await cog.handle_spawn_click(interaction, kind=self.kind)


class OwnerControlButton(discord.ui.DynamicItem[discord.ui.Button], template=_OWNER_CUSTOM_ID_RE):
    """One owner-control button; state (the action and session) lives entirely in its ``custom_id``."""

    _LABELS: dict[str, tuple[str, str]] = {
        "rename": ("Rename", "✏️"),
        "cap": ("Set cap", "👥"),
        "transfer": ("Transfer", "👑"),
        "end": ("End", "⏹️"),
    }
    _STYLES: dict[str, discord.ButtonStyle] = {"end": discord.ButtonStyle.danger}

    def __init__(self, session_id: UUID, action: str, *, locked: bool = False) -> None:
        if action == "locktoggle":
            label, emoji = ("Unlock", "🔓") if locked else ("Lock", "🔒")
        else:
            label, emoji = self._LABELS[action]
        super().__init__(
            discord.ui.Button(
                style=self._STYLES.get(action, discord.ButtonStyle.secondary),
                label=label,
                emoji=emoji,
                custom_id=f"sq:lfg:owner:{action}:{session_id}",
            )
        )
        self.session_id = session_id
        self.action = action

    @classmethod
    async def from_custom_id(
        cls, _interaction: discord.Interaction[SideQuestBot], _item: discord.ui.Item[Any], match: re.Match[str], /
    ) -> Self:
        """Rebuild the button from a dispatched component after a restart."""
        return cls(UUID(match["session_id"]), match["action"])

    async def callback(self, interaction: discord.Interaction[SideQuestBot]) -> None:
        """Route the click to :meth:`GroupFinderCog.handle_owner_action`."""
        cog = interaction.client.get_cog("GroupFinderCog")
        if isinstance(cog, GroupFinderCog):
            await cog.handle_owner_action(interaction, session_id=self.session_id, action=self.action)


class OpenVoiceButton(discord.ui.DynamicItem[discord.ui.Button], template=_EVENT_CUSTOM_ID_RE):
    """The "Open voice" button on an event thread or weekly-board card.

    Carries only the event id (Discord's 100-char custom_id limit can't safely fit a title too); the click handler
    looks the event's kind/title up from the API.
    """

    def __init__(self, event_id: UUID) -> None:
        super().__init__(
            discord.ui.Button(
                style=discord.ButtonStyle.success, label="Open voice", custom_id=build_open_voice_custom_id(event_id)
            )
        )
        self.event_id = event_id

    @classmethod
    async def from_custom_id(
        cls, _interaction: discord.Interaction[SideQuestBot], _item: discord.ui.Item[Any], match: re.Match[str], /
    ) -> Self:
        """Rebuild the button from a dispatched component after a restart."""
        return cls(UUID(match["event_id"]))

    async def callback(self, interaction: discord.Interaction[SideQuestBot]) -> None:
        """Route the click to :meth:`GroupFinderCog.handle_open_voice_click`."""
        cog = interaction.client.get_cog("GroupFinderCog")
        if isinstance(cog, GroupFinderCog):
            await cog.handle_open_voice_click(interaction, event_id=self.event_id)


class PugButton(discord.ui.DynamicItem[discord.ui.Button], template=_PUG_CUSTOM_ID_RE):
    """The ``#lfg`` panel's PUG group row; carries no state of its own."""

    def __init__(self) -> None:
        super().__init__(
            discord.ui.Button(
                style=discord.ButtonStyle.secondary, label="PUG group", emoji="🌐", custom_id=_PUG_CUSTOM_ID
            )
        )

    @classmethod
    async def from_custom_id(
        cls, _interaction: discord.Interaction[SideQuestBot], _item: discord.ui.Item[Any], _match: re.Match[str], /
    ) -> Self:
        """Rebuild the button from a dispatched component after a restart."""
        return cls()

    async def callback(self, interaction: discord.Interaction[SideQuestBot]) -> None:
        """Route the click to :meth:`GroupFinderCog.handle_pug_click`."""
        cog = interaction.client.get_cog("GroupFinderCog")
        if isinstance(cog, GroupFinderCog):
            await cog.handle_pug_click(interaction)


class PugKindSelect(discord.ui.Select["PugKindView"]):
    """Ephemeral, one-shot kind picker for a new PUG group."""

    def __init__(self, cog: GroupFinderCog) -> None:
        super().__init__(
            placeholder="Pick a group kind",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label=GROUP_KIND_LABELS[kind], value=kind.value, emoji=KIND_EMOJI[kind])
                for kind in GROUP_KINDS
            ],
        )
        self.cog = cog

    async def callback(self, interaction: discord.Interaction[SideQuestBot]) -> None:
        """Defer, then spawn the chosen kind as a PUG group via the cog."""
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.cog.handle_pug_kind_chosen(interaction, kind=EventKind(self.values[0]))


class PugKindView(discord.ui.View):
    """Ephemeral view holding :class:`PugKindSelect`, shown after a PUG group click."""

    def __init__(self, cog: GroupFinderCog) -> None:
        super().__init__(timeout=120)
        self.add_item(PugKindSelect(cog))


class _DetailsButton(discord.ui.Button):
    """The "Add details" button on a spawn confirmation; opens the details modal for the just-created group."""

    def __init__(self, cog: GroupFinderCog, session_id: UUID, kind: EventKind) -> None:
        super().__init__(style=discord.ButtonStyle.secondary, label="Add details")
        self.cog = cog
        self.session_id = session_id
        self.kind = kind

    async def callback(self, interaction: discord.Interaction[SideQuestBot]) -> None:
        """Look up the kind's current ping role (it may have changed since spawn) and open the modal."""
        if interaction.guild is None:
            return
        config = await self.cog.bot.configs.get(interaction.guild.id)
        ping_role_id = config.lfg_ping_role_ids.get(self.kind.value)
        await interaction.response.send_modal(AddDetailsModal(self.cog, self.session_id, ping_role_id))


class AddDetailsModal(discord.ui.Modal, title="Add details"):
    """Name/instance, note, needed roles, invites, and an opt-in ping for a group, all optional."""

    def __init__(self, cog: GroupFinderCog, session_id: UUID, ping_role_id: int | None) -> None:
        super().__init__()
        self.cog = cog
        self.session_id = session_id
        self.ping_role_id = ping_role_id
        self.name_input = discord.ui.TextInput(
            max_length=MAX_GROUP_NAME_LENGTH, required=False, placeholder="Deadmines"
        )
        self.note_input = discord.ui.TextInput(
            style=discord.TextStyle.paragraph, max_length=MAX_GROUP_NOTE_LENGTH, required=False
        )
        self.needs_group = discord.ui.CheckboxGroup(required=False, min_values=0, max_values=3, options=_NEED_OPTIONS)
        self.invite_select = discord.ui.UserSelect(
            required=False, min_values=0, max_values=10, placeholder="Invite members"
        )
        self.add_item(discord.ui.Label(text="Name / instance", component=self.name_input))
        self.add_item(discord.ui.Label(text="Note", component=self.note_input))
        self.add_item(discord.ui.Label(text="Need", component=self.needs_group))
        self.add_item(discord.ui.Label(text="Invite members", component=self.invite_select))
        self.ping_checkbox: discord.ui.Checkbox | None = None
        if ping_role_id is not None:
            self.ping_checkbox = discord.ui.Checkbox()
            self.add_item(discord.ui.Label(text="Ping the LFG role", component=self.ping_checkbox))

    async def on_submit(self, interaction: discord.Interaction[SideQuestBot]) -> None:
        """Defer, then apply the chosen details via the cog."""
        await interaction.response.defer(ephemeral=True, thinking=True)
        needs = [SignupRole(value) for value in self.needs_group.values]
        invited = [member for member in self.invite_select.values if isinstance(member, discord.Member)]
        ping = bool(self.ping_checkbox and self.ping_checkbox.value)
        await self.cog.apply_details(
            interaction,
            session_id=self.session_id,
            name=self.name_input.value or None,
            note=self.note_input.value or None,
            needs=needs,
            invited=invited,
            ping_role_id=self.ping_role_id if ping else None,
        )


class RenameModal(discord.ui.Modal, title="Rename group"):
    """Owner control: rename the channel (and the group's display name)."""

    def __init__(self, cog: GroupFinderCog, session_id: UUID) -> None:
        super().__init__()
        self.cog = cog
        self.session_id = session_id
        self.name_input = discord.ui.TextInput(max_length=MAX_GROUP_NAME_LENGTH, placeholder="Deadmines")
        self.add_item(discord.ui.Label(text="Name / instance", component=self.name_input))

    async def on_submit(self, interaction: discord.Interaction[SideQuestBot]) -> None:
        """Defer, then rename via the cog."""
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.cog.rename_group(interaction, session_id=self.session_id, name=self.name_input.value)


class CapModal(discord.ui.Modal, title="Set voice user limit"):
    """Owner control: change the channel's voice user limit."""

    def __init__(self, cog: GroupFinderCog, session_id: UUID) -> None:
        super().__init__()
        self.cog = cog
        self.session_id = session_id
        self.cap_input = discord.ui.TextInput(max_length=2, placeholder="40 (0 for no limit)")
        self.add_item(discord.ui.Label(text="User limit", component=self.cap_input))

    async def on_submit(self, interaction: discord.Interaction[SideQuestBot]) -> None:
        """Defer, then validate and apply the new cap via the cog."""
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not self.cap_input.value.isdigit():
            await interaction.followup.send("That's not a number.", ephemeral=True)
            return
        await self.cog.set_cap(interaction, session_id=self.session_id, cap=int(self.cap_input.value))


class TransferModal(discord.ui.Modal, title="Transfer ownership"):
    """Owner control: hand the group to another member."""

    def __init__(self, cog: GroupFinderCog, session_id: UUID) -> None:
        super().__init__()
        self.cog = cog
        self.session_id = session_id
        self.member_select = discord.ui.UserSelect(min_values=1, max_values=1, required=True)
        self.add_item(discord.ui.Label(text="New owner", component=self.member_select))

    async def on_submit(self, interaction: discord.Interaction[SideQuestBot]) -> None:
        """Defer, then transfer via the cog."""
        await interaction.response.defer(ephemeral=True, thinking=True)
        target = self.member_select.values[0]
        if not isinstance(target, discord.Member) or target.bot:
            await interaction.followup.send("Pick a human member.", ephemeral=True)
            return
        await self.cog.transfer_group(interaction, session_id=self.session_id, new_owner=target)


class EndConfirmView(discord.ui.View):
    """Ephemeral confirm/cancel for the End owner control."""

    def __init__(self, cog: GroupFinderCog, session_id: UUID) -> None:
        super().__init__(timeout=60)
        self.cog = cog
        self.session_id = session_id

    @discord.ui.button(label="End group", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction[SideQuestBot], _button: discord.ui.Button) -> None:
        """End the group."""
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.cog.end_group(interaction, session_id=self.session_id)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction[SideQuestBot], _button: discord.ui.Button) -> None:
        """Dismiss without ending the group."""
        await interaction.response.edit_message(content="Cancelled.", view=None)


class GroupFinderCog(commands.Cog):
    """The ``#lfg`` panel, group voice channel lifecycle, owner controls, presence tracking and teardown."""

    lfg_group = app_commands.Group(
        name="lfg",
        description="Configure group voice channels.",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    def __init__(self, bot: SideQuestBot) -> None:
        self.bot = bot
        self._groups: dict[UUID, LiveGroup] = {}
        self._recovered_guild_ids: set[int] = set()
        self._card_debounce: dict[UUID, asyncio.Task[None]] = {}
        self._spawn_lock = asyncio.Lock()
        """Held from the limit checks through registering the new group, so two clicks can't share a name or limit."""
        self._pug_orphan_since: dict[int, datetime] = {}
        """Per-member clock for :func:`pug_sweep_decision`'s ``orphaned_since``. Reset by a restart (documented)."""
        self._pug_kick_forbidden_warned: set[int] = set()
        self._presence_tick.start()
        self._teardown_tick.start()
        self._daily_purge.start()
        self._pug_sweep.start()

    async def cog_unload(self) -> None:
        """Stop every loop and any pending debounced card refresh."""
        self._presence_tick.cancel()
        self._teardown_tick.cancel()
        self._daily_purge.cancel()
        self._pug_sweep.cancel()
        for task in self._card_debounce.values():
            task.cancel()

    def linked_channel_id(self, event_id: UUID) -> int | None:
        """The voice channel id of a live group linked to ``event_id``, or ``None`` if there isn't one."""
        return next((live.channel_id for live in self._groups.values() if live.event_id == event_id), None)

    def _group_by_channel(self, channel_id: int) -> LiveGroup | None:
        return next((live for live in self._groups.values() if live.channel_id == channel_id), None)

    def _owned_live_group(self, member_id: int) -> LiveGroup | None:
        return next(
            (live for live in self._groups.values() if live.owner_id == member_id and live.event_id is None), None
        )

    # --- Startup / recovery --------------------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """Re-ensure the panel for every guild that has one configured."""
        for guild in list(self.bot.guilds):
            await self._ensure_recovered(guild)
            config = await self.bot.configs.get(guild.id)
            if config.lfg_channel_id is None:
                continue
            channel = guild.get_channel(config.lfg_channel_id)
            if isinstance(channel, discord.TextChannel):
                await self._ensure_panel(guild, channel)

    async def _ensure_recovered(self, guild: discord.Guild) -> None:
        """Adopt still-live groups from ``GET /groups/open`` once per guild per process."""
        if guild.id in self._recovered_guild_ids:
            return
        self._recovered_guild_ids.add(guild.id)
        try:
            sessions = await self.bot.api.list_open_groups(guild.id)
        except NotFoundError:
            return
        except APIError:
            logger.warning("Failed to recover open groups for guild %s; will retry", guild.id, exc_info=True)
            self._recovered_guild_ids.discard(guild.id)
            return
        for session in sessions:
            await self._recover_session(guild, session)

    async def _recover_session(self, guild: discord.Guild, session: GroupSessionSchema) -> None:
        channel = guild.get_channel(int(session.discord_channel_id))
        if not isinstance(channel, discord.VoiceChannel):
            await self._close_missing(guild, session)
            return
        now = datetime.now(UTC)
        live = LiveGroup(
            session_id=session.id,
            guild_id=guild.id,
            channel_id=channel.id,
            kind=session.kind,
            creator_id=int(session.creator_discord_id),
            owner_id=int(session.owner_discord_id),
            channel_name=session.channel_name,
            name=session.name,
            note=session.note,
            needs=list(session.needs),
            cap=session.cap,
            event_id=session.event_id,
            card_message_id=int(session.card_message_id) if session.card_message_id else None,
            started_at=session.started_at,
            minutes={int(member.discord_id): member.minutes for member in session.members},
            pairs={pair_key(int(p.discord_id_a), int(p.discord_id_b)): p.minutes for p in session.pairs},
            display_names={int(member.discord_id): member.display_name for member in session.members},
            peak_size=session.peak_size,
            grouped_minutes=session.grouped_minutes,
            # The invite link itself isn't persisted, so a recovered PUG group's owner controls show no link until
            # the owner is told to make a fresh one (there's no API-side invite store to recreate it from).
            pug_role_id=int(session.pug_role_id) if session.pug_role_id else None,
        )
        present = {member.id for member in channel.members if not member.bot}
        live.ever_joined = bool(present) or bool(live.minutes)
        live.empty_since = None if present else now
        for member_id in present:
            live.join_order.setdefault(member_id, now)
        self._groups[session.id] = live
        await self._refresh_card(guild, live)

    async def _close_missing(self, guild: discord.Guild, session: GroupSessionSchema) -> None:
        try:
            await self.bot.api.close_group(
                guild.id,
                session.id,
                GroupSessionClose(
                    end_reason=GroupEndReason.LOST,
                    presence=GroupPresenceReport(
                        peak_size=session.peak_size, grouped_minutes=session.grouped_minutes, members=[], pairs=[]
                    ),
                ),
            )
        except APIError:
            logger.warning("Failed to close missing group session %s", session.id, exc_info=True)
            return
        await self._delete_card_message(guild, int(session.card_message_id) if session.card_message_id else None)

    # --- Panel -----------------------------------------------------------------------------------------------

    async def _ensure_panel(self, guild: discord.Guild, channel: discord.TextChannel) -> None:
        """Draw the panel over the saved message, or post a fresh one (also replacing a pre-layout embed panel)."""
        config = await self.bot.configs.get(guild.id)
        if config.lfg_panel_message_id is not None:
            try:
                existing = await channel.fetch_message(config.lfg_panel_message_id)
                if existing.flags.components_v2:
                    await existing.edit(view=build_panel_layout(), attachments=[banner_file()])
                    return
                await existing.delete()  # an embed panel can't be edited into a layout; replace it
            except discord.NotFound:
                pass
            except discord.HTTPException:
                logger.warning("Failed to redraw the LFG panel in guild %s", guild.id, exc_info=True)
                return
        try:
            message = await channel.send(
                view=build_panel_layout(), file=banner_file(), allowed_mentions=discord.AllowedMentions.none()
            )
        except discord.HTTPException:
            logger.warning("Failed to post the LFG panel in guild %s", guild.id, exc_info=True)
            return
        await self.bot.configs.update(guild.id, GuildConfigUpdate(lfg_panel_message_id=message.id))

    # --- Spawning ----------------------------------------------------------------------------------------------

    async def handle_spawn_click(self, interaction: discord.Interaction[SideQuestBot], *, kind: EventKind) -> None:
        """A panel button click: permission/limit checks, then create the channel and its session."""
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            return
        config = await self.bot.configs.get(guild.id)
        if not can_spawn(member, config.roster_role_id):
            await interaction.followup.send("Only roster-confirmed members can start a group.", ephemeral=True)
            return
        async with self._spawn_lock:
            await self._ensure_recovered(guild)
            if self._owned_live_group(member.id) is not None:
                await interaction.followup.send(
                    "You already have a live group; end it before starting another.", ephemeral=True
                )
                return
            if len([live for live in self._groups.values() if live.guild_id == guild.id]) >= MAX_LIVE_GROUPS:
                await interaction.followup.send(
                    f"{MAX_LIVE_GROUPS} groups are already live; try again shortly.", ephemeral=True
                )
                return
            category = await self._lfg_category(interaction, config)
            if category is None:
                return
            number = allocate_group_number(extract_taken_numbers(self._taken_names(category), GROUP_KIND_PREFIX[kind]))
            name = default_channel_name(kind, number)
            await self._create_group(
                interaction, kind=kind, category=category, name=name, event_id=None, display_name=None
            )

    async def handle_pug_click(self, interaction: discord.Interaction[SideQuestBot]) -> None:
        """The PUG group panel button: permission/setup checks, then ask which kind to spawn."""
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            return
        config = await self.bot.configs.get(guild.id)
        if not can_spawn(member, config.roster_role_id):
            await interaction.followup.send("Only roster-confirmed members can start a group.", ephemeral=True)
            return
        if config.pug_role_id is None:
            await interaction.followup.send(
                "PUG groups aren't set up on this server yet; ask an officer to run `/lfg pugrole`.", ephemeral=True
            )
            return
        await interaction.followup.send("Pick a kind for this PUG group:", view=PugKindView(self), ephemeral=True)

    async def handle_pug_kind_chosen(self, interaction: discord.Interaction[SideQuestBot], *, kind: EventKind) -> None:
        """The PUG kind select: re-checked permission/setup, then create the channel, role, overwrite and invite."""
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            return
        config = await self.bot.configs.get(guild.id)
        if not can_spawn(member, config.roster_role_id):
            await interaction.followup.send("Only roster-confirmed members can start a group.", ephemeral=True)
            return
        if config.pug_role_id is None:
            await interaction.followup.send(
                "PUG groups aren't set up on this server yet; ask an officer to run `/lfg pugrole`.", ephemeral=True
            )
            return
        async with self._spawn_lock:
            await self._ensure_recovered(guild)
            if self._owned_live_group(member.id) is not None:
                await interaction.followup.send(
                    "You already have a live group; end it before starting another.", ephemeral=True
                )
                return
            if len([live for live in self._groups.values() if live.guild_id == guild.id]) >= MAX_LIVE_GROUPS:
                await interaction.followup.send(
                    f"{MAX_LIVE_GROUPS} groups are already live; try again shortly.", ephemeral=True
                )
                return
            category = await self._lfg_category(interaction, config)
            if category is None:
                return
            prefix = f"{PUG_PREFIX}-{GROUP_KIND_PREFIX[kind]}"
            number = allocate_group_number(extract_taken_numbers(self._taken_names(category), prefix))
            name = pug_channel_name(kind, number)
            await self._create_group(
                interaction, kind=kind, category=category, name=name, event_id=None, display_name=None, pug=True
            )

    async def handle_open_voice_click(self, interaction: discord.Interaction[SideQuestBot], *, event_id: UUID) -> None:
        """An "Open voice" click: join the existing linked group, or spawn one."""
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            return
        config = await self.bot.configs.get(guild.id)
        if not can_spawn(member, config.roster_role_id):
            await interaction.followup.send(
                "Only roster-confirmed members can open voice for this event.", ephemeral=True
            )
            return
        async with self._spawn_lock:
            await self._ensure_recovered(guild)
            existing = next(
                (live for live in self._groups.values() if live.guild_id == guild.id and live.event_id == event_id),
                None,
            )
            if existing is not None:
                join_url = f"https://discord.com/channels/{guild.id}/{existing.channel_id}"
                await interaction.followup.send(f"Already live: {join_url}", ephemeral=True)
                return
            if len([live for live in self._groups.values() if live.guild_id == guild.id]) >= MAX_LIVE_GROUPS:
                await interaction.followup.send(
                    f"{MAX_LIVE_GROUPS} groups are already live; try again shortly.", ephemeral=True
                )
                return
            event = await self._fetch_event_or_notify(interaction, event_id)
            if event is None:
                return
            kind = mapped_group_kind(event.kind)
            category = await self._lfg_category(interaction, config)
            if category is None:
                return
            slug = slugify(event.title)
            name = named_channel_name(kind, slug, self._taken_names(category))
            await self._create_group(
                interaction,
                kind=kind,
                category=category,
                name=name,
                event_id=event_id,
                display_name=event.title[:MAX_GROUP_NAME_LENGTH],
            )

    def _taken_names(self, category: discord.CategoryChannel) -> set[str]:
        """Channel names in use: the category's cached channels plus every live group (the cache can lag a create)."""
        return {channel.name for channel in category.channels} | {live.channel_name for live in self._groups.values()}

    async def _fetch_event_or_notify(
        self, interaction: discord.Interaction[SideQuestBot], event_id: UUID
    ) -> EventSummary | None:
        """``event_id``'s current headline fields, or ``None`` after telling the clicker why it's not available."""
        try:
            return await self.bot.api.get_event(event_id)
        except NotFoundError:
            await interaction.followup.send("That event is gone.", ephemeral=True)
            return None
        except APIError:
            logger.warning("Failed to fetch event %s for Open voice", event_id, exc_info=True)
            await interaction.followup.send("Couldn't reach the guild records; try again shortly.", ephemeral=True)
            return None

    async def _lfg_category(
        self, interaction: discord.Interaction[SideQuestBot], config: GuildConfigSchema
    ) -> discord.CategoryChannel | None:
        guild = interaction.guild
        lfg_channel_id = config.lfg_channel_id
        if guild is None or lfg_channel_id is None:
            await interaction.followup.send("Group voice isn't set up on this server.", ephemeral=True)
            return None
        lfg_channel = guild.get_channel(lfg_channel_id)
        if not isinstance(lfg_channel, discord.TextChannel) or lfg_channel.category is None:
            await interaction.followup.send("The #lfg channel needs a category; ask an officer.", ephemeral=True)
            return None
        return lfg_channel.category

    async def _create_group(
        self,
        interaction: discord.Interaction[SideQuestBot],
        *,
        kind: EventKind,
        category: discord.CategoryChannel,
        name: str,
        event_id: UUID | None,
        display_name: str | None,
        pug: bool = False,
    ) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            return
        cap = GROUP_KIND_DEFAULT_CAP[kind]
        try:
            channel = await guild.create_voice_channel(
                name,
                category=category,
                user_limit=cap or 0,
                reason=f"LFG: {member} started a {GROUP_KIND_LABELS[kind]} group",
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "I can't create voice channels here; ask an officer to check my permissions.", ephemeral=True
            )
            return
        with contextlib.suppress(discord.HTTPException):
            await channel.edit(sync_permissions=True)
        pug_extras: tuple[discord.Role, str] | None = None
        if pug:
            config = await self.bot.configs.get(guild.id)
            pug_extras = await self._setup_pug_group(guild, channel, config)
            if pug_extras is None:
                with contextlib.suppress(discord.HTTPException):
                    await channel.delete(reason="Failed to set up PUG group")
                await interaction.followup.send(
                    "Couldn't set up the PUG group — I may be missing Manage Roles or Create Invite; "
                    "try again shortly or ask an officer to check my permissions.",
                    ephemeral=True,
                )
                return
        pug_role, invite_code = pug_extras if pug_extras is not None else (None, None)
        try:
            session = await self.bot.api.create_group(
                guild.id,
                GroupSessionCreate(
                    discord_channel_id=str(channel.id),
                    channel_name=channel.name,
                    kind=kind,
                    creator_discord_id=str(member.id),
                    cap=cap,
                    event_id=event_id,
                    name=display_name,
                    pug_role_id=str(pug_role.id) if pug_role is not None else None,
                ),
            )
        except APIError:
            logger.warning("Failed to record group session for channel %s", channel.id, exc_info=True)
            with contextlib.suppress(discord.HTTPException):
                await channel.delete(reason="Failed to record group session")
            if pug_role is not None:
                with contextlib.suppress(discord.HTTPException):
                    await pug_role.delete(reason="Failed to record group session")
            if invite_code is not None:
                with contextlib.suppress(discord.HTTPException):
                    await self.bot.http.delete_invite(invite_code, reason="Failed to record group session")
            await interaction.followup.send("Couldn't reach the guild records; try again shortly.", ephemeral=True)
            return
        live = LiveGroup(
            session_id=session.id,
            guild_id=guild.id,
            channel_id=channel.id,
            kind=kind,
            creator_id=member.id,
            owner_id=member.id,
            channel_name=channel.name,
            name=display_name,
            note=None,
            needs=[],
            cap=cap,
            event_id=event_id,
            card_message_id=None,
            started_at=session.started_at,
            pug_role_id=pug_role.id if pug_role is not None else None,
            invite_code=invite_code,
        )
        live.empty_since = datetime.now(UTC)
        self._groups[session.id] = live
        await self._post_owner_controls(channel, live)
        await self._refresh_card(guild, live)
        join_url = f"https://discord.com/channels/{guild.id}/{channel.id}"
        if event_id is not None:
            await interaction.followup.send(f"Opened {channel.mention}: {join_url}", ephemeral=True)
            return
        if pug:
            await interaction.followup.send(
                f"{KIND_EMOJI[kind]} {channel.mention} is yours. Share this invite link with outside players — "
                f"they'll see only this group and #sign-the-ledger: {live.invite_url}",
                ephemeral=True,
            )
            return
        view = discord.ui.View(timeout=180)
        view.add_item(discord.ui.Button(style=discord.ButtonStyle.link, label="Join", url=join_url))
        view.add_item(_DetailsButton(self, session.id, kind))
        await interaction.followup.send(
            f"{KIND_EMOJI[kind]} {channel.mention} is yours. Hop in, or **Add details** first: a name, a note, the "
            "roles you need, and who to ping. All optional.",
            view=view,
            ephemeral=True,
        )

    async def _setup_pug_group(
        self, guild: discord.Guild, channel: discord.VoiceChannel, config: GuildConfigSchema
    ) -> tuple[discord.Role, str] | None:
        """Create a PUG group's per-group role, overwrite, and invite link.

        Grants ``role`` access to ``channel``, then invites it plus the guild PUG role on a fresh invite link.
        Cleans up anything it created on failure and returns ``None``.
        """
        if config.pug_role_id is None:
            return None
        pug_role = guild.get_role(config.pug_role_id)
        if pug_role is None:
            return None
        try:
            group_role = await guild.create_role(
                name=f"{PUG_ROLE_NAME_PREFIX}{channel.name}",
                mentionable=False,
                hoist=False,
                reason="LFG: PUG group role",
            )
        except discord.Forbidden:
            return None
        try:
            await channel.set_permissions(
                group_role,
                view_channel=True,
                connect=True,
                speak=True,
                send_messages=True,
                read_message_history=True,
                use_voice_activation=True,
                reason="LFG: PUG group access",
            )
        except discord.Forbidden:
            with contextlib.suppress(discord.HTTPException):
                await group_role.delete(reason="LFG: PUG group setup failed")
            return None
        try:
            data = await self.bot.http.request(
                Route("POST", "/channels/{channel_id}/invites", channel_id=channel.id),
                reason="LFG: PUG group invite",
                json={
                    "max_age": PUG_INVITE_MAX_AGE_SECONDS,
                    "max_uses": 0,
                    "unique": True,
                    "role_ids": [str(pug_role.id), str(group_role.id)],
                },
            )
        except discord.HTTPException:
            logger.warning("Failed to create PUG invite for channel %s", channel.id, exc_info=True)
            with contextlib.suppress(discord.HTTPException):
                await group_role.delete(reason="LFG: PUG group setup failed")
            return None
        return group_role, data["code"]

    async def _post_owner_controls(self, channel: discord.VoiceChannel, live: LiveGroup) -> None:
        try:
            await channel.send(
                view=build_owner_controls_layout(
                    live.session_id, kind=live.kind, locked=live.locked, invite_url=live.invite_url
                ),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            logger.warning("Failed to post owner controls for group %s", live.session_id, exc_info=True)

    # --- Add details -------------------------------------------------------------------------------------------

    async def apply_details(
        self,
        interaction: discord.Interaction[SideQuestBot],
        *,
        session_id: UUID,
        name: str | None,
        note: str | None,
        needs: list[SignupRole],
        invited: list[discord.Member],
        ping_role_id: int | None,
    ) -> None:
        """Apply the Add details modal: only the owner or Manage Channels may."""
        guild = interaction.guild
        live = self._groups.get(session_id)
        if guild is None or live is None:
            await interaction.followup.send("That group isn't live anymore.", ephemeral=True)
            return
        member = interaction.user
        if not isinstance(member, discord.Member) or (
            member.id != live.owner_id and not member.guild_permissions.manage_channels
        ):
            await interaction.followup.send("Only the group's owner can change its details.", ephemeral=True)
            return
        role = guild.get_role(ping_role_id) if ping_role_id else None
        channel: discord.VoiceChannel | None = None
        reply = "Details updated."
        async with live.lock:
            found = guild.get_channel(live.channel_id)
            if not isinstance(found, discord.VoiceChannel):
                await interaction.followup.send("That channel is gone.", ephemeral=True)
                return
            channel = found
            live.note = note
            live.needs = needs
            live.invited_ids |= {invitee.id for invitee in invited}
            wait_minutes = rename_cooldown_minutes(live.rename_times, datetime.now(UTC)) if name else 0
            if wait_minutes:
                reply = (
                    "Details updated. Discord allows two channel renames per 10 minutes, so the channel keeps its "
                    f"name for now; rename it from the group controls in {wait_minutes} min."
                )
            elif name:
                existing = {c.name for c in channel.category.channels} if channel.category else set()
                existing.discard(channel.name)
                new_name = named_channel_name(live.kind, slugify(name), existing)
                try:
                    await channel.edit(name=new_name, reason=f"LFG: named by {member}")
                except discord.HTTPException:
                    logger.warning("Failed to rename group channel %s", channel.id, exc_info=True)
                else:
                    live.channel_name = new_name
                    live.rename_times.append(datetime.now(UTC))
            if name:
                live.name = name
            await self._push_update(guild, live)
        await self._refresh_card(guild, live)
        mentions = [invitee for invitee in invited if not invitee.bot]
        if (mentions or role) and channel is not None:
            content = " ".join([invitee.mention for invitee in mentions] + ([role.mention] if role else []))
            allowed = discord.AllowedMentions(users=mentions, roles=[role] if role else [], everyone=False)
            with contextlib.suppress(discord.HTTPException):
                await channel.send(f"{content} — join for {live.display_name}.", allowed_mentions=allowed)
        await interaction.followup.send(reply, ephemeral=True)

    # --- Owner controls ------------------------------------------------------------------------------------

    async def handle_owner_action(
        self, interaction: discord.Interaction[SideQuestBot], *, session_id: UUID, action: str
    ) -> None:
        """Dispatch one owner-control click, after checking the clicker is the owner or Manage Channels."""
        live = self._groups.get(session_id)
        if live is None:
            await interaction.response.send_message("That group isn't live anymore.", ephemeral=True)
            return
        member = interaction.user
        if not isinstance(member, discord.Member) or (
            member.id != live.owner_id and not member.guild_permissions.manage_channels
        ):
            await interaction.response.send_message("Only the group's owner can do that.", ephemeral=True)
            return
        if action == "rename":
            await interaction.response.send_modal(RenameModal(self, session_id))
        elif action == "cap":
            await interaction.response.send_modal(CapModal(self, session_id))
        elif action == "locktoggle":
            await interaction.response.defer(ephemeral=True, thinking=True)
            await self._toggle_lock(interaction, live)
        elif action == "transfer":
            await interaction.response.send_modal(TransferModal(self, session_id))
        elif action == "end":
            await interaction.response.send_message(
                "End this group? Everyone will be disconnected.", view=EndConfirmView(self, session_id), ephemeral=True
            )

    async def _toggle_lock(self, interaction: discord.Interaction[SideQuestBot], live: LiveGroup) -> None:
        guild = interaction.guild
        if guild is None:
            return
        channel = guild.get_channel(live.channel_id)
        if not isinstance(channel, discord.VoiceChannel):
            await interaction.followup.send("That channel is gone.", ephemeral=True)
            return
        config = await self.bot.configs.get(guild.id)
        async with live.lock:
            if live.locked:
                with contextlib.suppress(discord.Forbidden):
                    await channel.edit(sync_permissions=True, reason="LFG: unlocked")
                live.locked = False
            else:
                overwrites = dict(channel.overwrites)
                overwrites[guild.default_role] = discord.PermissionOverwrite(connect=False)
                if config.roster_role_id:
                    role = guild.get_role(config.roster_role_id)
                    if role is not None:
                        overwrites[role] = discord.PermissionOverwrite(connect=False)
                for member_id in {member.id for member in channel.members} | live.invited_ids:
                    allowed_member = guild.get_member(member_id)
                    if allowed_member is not None:
                        overwrites[allowed_member] = discord.PermissionOverwrite(connect=True)
                try:
                    await channel.edit(overwrites=overwrites, reason="LFG: locked")
                except discord.Forbidden:
                    await interaction.followup.send("I need Manage Roles to lock this channel.", ephemeral=True)
                    return
                live.locked = True
        if interaction.message is not None:
            with contextlib.suppress(discord.HTTPException):
                await interaction.message.edit(
                    view=build_owner_controls_layout(
                        live.session_id, kind=live.kind, locked=live.locked, invite_url=live.invite_url
                    )
                )
        await self._refresh_card(guild, live)
        await interaction.followup.send("Locked." if live.locked else "Unlocked.", ephemeral=True)

    async def rename_group(
        self, interaction: discord.Interaction[SideQuestBot], *, session_id: UUID, name: str
    ) -> None:
        """Owner control: rename, respecting Discord's rename cooldown."""
        guild = interaction.guild
        live = self._groups.get(session_id)
        if guild is None or live is None:
            await interaction.followup.send("That group isn't live anymore.", ephemeral=True)
            return
        cooldown = rename_cooldown_minutes(live.rename_times, datetime.now(UTC))
        if cooldown:
            await interaction.followup.send(f"Discord limits renames; try again in {cooldown} min.", ephemeral=True)
            return
        async with live.lock:
            channel = guild.get_channel(live.channel_id)
            if not isinstance(channel, discord.VoiceChannel):
                await interaction.followup.send("That channel is gone.", ephemeral=True)
                return
            existing = {c.name for c in channel.category.channels} if channel.category else set()
            existing.discard(channel.name)
            new_name = named_channel_name(live.kind, slugify(name), existing)
            try:
                await channel.edit(name=new_name, reason=f"LFG: renamed by {interaction.user}")
            except discord.HTTPException:
                await interaction.followup.send("Couldn't rename the channel; try again shortly.", ephemeral=True)
                return
            live.channel_name = new_name
            live.name = name
            live.rename_times.append(datetime.now(UTC))
            await self._push_update(guild, live)
        await self._refresh_card(guild, live)
        await interaction.followup.send(f"Renamed to {new_name}.", ephemeral=True)

    async def set_cap(self, interaction: discord.Interaction[SideQuestBot], *, session_id: UUID, cap: int) -> None:
        """Owner control: change the voice user limit (0 clears it)."""
        guild = interaction.guild
        live = self._groups.get(session_id)
        if guild is None or live is None:
            await interaction.followup.send("That group isn't live anymore.", ephemeral=True)
            return
        if not (0 <= cap <= MAX_GROUP_CAP):
            await interaction.followup.send(f"Pick 0 (no limit) to {MAX_GROUP_CAP}.", ephemeral=True)
            return
        async with live.lock:
            channel = guild.get_channel(live.channel_id)
            if not isinstance(channel, discord.VoiceChannel):
                await interaction.followup.send("That channel is gone.", ephemeral=True)
                return
            try:
                await channel.edit(user_limit=cap)
            except discord.HTTPException:
                await interaction.followup.send("Couldn't set the user limit; try again shortly.", ephemeral=True)
                return
            live.cap = cap or None
            await self._push_update(guild, live)
        await self._refresh_card(guild, live)
        await interaction.followup.send(f"User limit set to {cap or 'none'}.", ephemeral=True)

    async def transfer_group(
        self, interaction: discord.Interaction[SideQuestBot], *, session_id: UUID, new_owner: discord.Member
    ) -> None:
        """Owner control: hand ownership to another member."""
        guild = interaction.guild
        live = self._groups.get(session_id)
        if guild is None or live is None:
            await interaction.followup.send("That group isn't live anymore.", ephemeral=True)
            return
        async with live.lock:
            live.owner_id = new_owner.id
            await self._push_update(guild, live)
        await self._refresh_card(guild, live)
        await interaction.followup.send(f"{new_owner.mention} now owns this group.", ephemeral=True)

    async def end_group(self, interaction: discord.Interaction[SideQuestBot], *, session_id: UUID) -> None:
        """Owner control: tear the group down now, as if the owner had pressed End and confirmed."""
        guild = interaction.guild
        live = self._groups.get(session_id)
        if guild is None or live is None:
            await interaction.followup.send("That group isn't live anymore.", ephemeral=True)
            return
        await self._teardown(guild, live, reason=GroupEndReason.OWNER)
        await interaction.followup.send("Group ended.", ephemeral=True)

    # --- Live card -----------------------------------------------------------------------------------------

    def _debounce_card(self, guild: discord.Guild, live: LiveGroup) -> None:
        pending = self._card_debounce.get(live.session_id)
        if pending is not None and not pending.done():
            pending.cancel()
        self._card_debounce[live.session_id] = asyncio.create_task(self._debounced_refresh(guild, live))

    async def _debounced_refresh(self, guild: discord.Guild, live: LiveGroup) -> None:
        try:
            await asyncio.sleep(CARD_DEBOUNCE_SECONDS)
        except asyncio.CancelledError:
            return
        await self._refresh_card(guild, live)

    async def _refresh_card(self, guild: discord.Guild, live: LiveGroup) -> None:
        config = await self.bot.configs.get(guild.id)
        if config.lfg_channel_id is None:
            return
        channel = guild.get_channel(config.lfg_channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        voice_channel = guild.get_channel(live.channel_id)
        present = (
            [m.id for m in voice_channel.members if not m.bot]
            if isinstance(voice_channel, discord.VoiceChannel)
            else []
        )
        far_future = datetime.max.replace(tzinfo=UTC)
        present.sort(key=lambda member_id: live.join_order.get(member_id, far_future))
        content = build_card_content(
            kind=live.kind,
            display_name=live.display_name,
            member_ids=present,
            cap=live.cap,
            needs=live.needs,
            note=live.note,
            owner_id=live.owner_id,
            locked=live.locked,
            started_at=live.started_at,
        )
        if content == live.last_card:
            return
        live.last_card = content
        layout = build_card_layout(
            content, kind=live.kind, join_url=f"https://discord.com/channels/{guild.id}/{live.channel_id}"
        )
        if live.card_message_id is not None:
            message = channel.get_partial_message(live.card_message_id)
            try:
                await message.edit(view=layout)
            except discord.NotFound:
                pass
            except discord.HTTPException:
                # e.g. a plain-text card from before cards became layouts: replace it
                with contextlib.suppress(discord.HTTPException):
                    await message.delete()
            else:
                return
        try:
            posted = await channel.send(view=layout, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            logger.warning("Failed to post the LFG card for group %s", live.session_id, exc_info=True)
            live.last_card = None
            return
        live.card_message_id = posted.id
        await self._push_update(guild, live)

    async def _delete_card_message(self, guild: discord.Guild, card_message_id: int | None) -> None:
        if card_message_id is None:
            return
        config = await self.bot.configs.get(guild.id)
        if config.lfg_channel_id is None:
            return
        channel = guild.get_channel(config.lfg_channel_id)
        if isinstance(channel, discord.TextChannel):
            with contextlib.suppress(discord.NotFound, discord.Forbidden):
                await channel.get_partial_message(card_message_id).delete()

    # --- API push --------------------------------------------------------------------------------------------

    async def _push_update(self, guild: discord.Guild, live: LiveGroup) -> None:
        try:
            await self.bot.api.update_group(
                guild.id,
                live.session_id,
                GroupSessionUpdate(
                    channel_name=live.channel_name,
                    name=live.name,
                    note=live.note,
                    needs=live.needs,
                    cap=live.cap,
                    owner_discord_id=str(live.owner_id),
                    card_message_id=str(live.card_message_id) if live.card_message_id else None,
                ),
            )
        except APIError:
            logger.warning("Failed to push group update for session %s", live.session_id, exc_info=True)

    async def _push_presence(self, guild: discord.Guild, live: LiveGroup) -> None:
        try:
            await self.bot.api.report_group_presence(guild.id, live.session_id, build_presence_report(live))
        except APIError:
            logger.warning("Failed to push group presence for session %s", live.session_id, exc_info=True)

    # --- Presence tick -----------------------------------------------------------------------------------------

    @tasks.loop(seconds=PRESENCE_TICK_SECONDS)
    async def _presence_tick(self) -> None:
        for guild in list(self.bot.guilds):
            await self._ensure_recovered(guild)
        now = datetime.now(UTC)
        for live in list(self._groups.values()):
            guild = self.bot.get_guild(live.guild_id)
            if guild is None:
                continue
            await self._tick_one(guild, live, now)

    @_presence_tick.before_loop
    async def _before_presence_tick(self) -> None:
        await self.bot.wait_until_ready()

    async def _tick_one(self, guild: discord.Guild, live: LiveGroup, now: datetime) -> None:
        channel = guild.get_channel(live.channel_id)
        if not isinstance(channel, discord.VoiceChannel):
            return
        async with live.lock:
            present = {member.id for member in channel.members if not member.bot}
            await self._update_presence_state(guild, live, present, now)
            outsiders: set[int] = set()
            if live.pug_role_id is not None:
                # Only a PUG group can hold outside players. A failed config read counts them for one minute rather
                # than letting the error stop this loop.
                with contextlib.suppress(APIError):
                    config = await self.bot.configs.get(guild.id)
                    outsiders = self._pug_member_ids(guild, config.pug_role_id)
            counted = present - outsiders
            live.minutes, live.pairs, live.peak_size, live.grouped_minutes = tick_presence(
                present_ids=counted,
                minutes=live.minutes,
                pairs=live.pairs,
                peak_size=live.peak_size,
                grouped_minutes=live.grouped_minutes,
            )
            if live.last_push_at is None or (now - live.last_push_at).total_seconds() >= PRESENCE_PUSH_INTERVAL_SECONDS:
                await self._push_presence(guild, live)
                live.last_push_at = now

    @staticmethod
    def _pug_member_ids(guild: discord.Guild, pug_role_id: int | None) -> set[int]:
        """Current holders of the guild's PUG role.

        Excluded from group-stats minutes; they're outside players, not guild members.
        """
        if pug_role_id is None:
            return set()
        role = guild.get_role(pug_role_id)
        return {member.id for member in role.members} if role is not None else set()

    async def _update_presence_state(
        self, guild: discord.Guild, live: LiveGroup, present: set[int], now: datetime
    ) -> None:
        """Update join order, display names, emptiness, and owner succession for one tick/voice-state change."""
        for member_id in present:
            member = guild.get_member(member_id)
            if member is not None:
                live.display_names[member_id] = str(member)
            live.join_order.setdefault(member_id, now)
        for member_id in [tracked for tracked in live.join_order if tracked not in present]:
            del live.join_order[member_id]
        if present:
            live.ever_joined = True
            live.empty_since = None
        elif live.empty_since is None:
            live.empty_since = now
        if live.owner_id not in present and present:
            successor = next_owner(live.owner_id, sorted(present), live.join_order)
            if successor is not None:
                live.owner_id = successor
                await self._push_update(guild, live)

    @commands.Cog.listener()
    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ) -> None:
        """Update emptiness/succession promptly on join/leave, and debounce the live card."""
        for channel_id in {channel.id for channel in (before.channel, after.channel) if channel is not None}:
            live = self._group_by_channel(channel_id)
            if live is None:
                continue
            guild = member.guild
            channel = guild.get_channel(live.channel_id)
            if not isinstance(channel, discord.VoiceChannel):
                continue
            async with live.lock:
                present = {m.id for m in channel.members if not m.bot}
                await self._update_presence_state(guild, live, present, datetime.now(UTC))
            self._debounce_card(guild, live)

    # --- Teardown ----------------------------------------------------------------------------------------------

    @tasks.loop(seconds=TEARDOWN_CHECK_SECONDS)
    async def _teardown_tick(self) -> None:
        now = datetime.now(UTC)
        for live in list(self._groups.values()):
            guild = self.bot.get_guild(live.guild_id)
            if guild is None:
                continue
            channel = guild.get_channel(live.channel_id)
            if channel is None:
                await self._teardown(guild, live, reason=GroupEndReason.LOST)
                continue
            if should_tear_down(live.empty_since, now):
                reason = GroupEndReason.EMPTY if live.ever_joined else GroupEndReason.UNJOINED
                await self._teardown(guild, live, reason=reason)

    @_teardown_tick.before_loop
    async def _before_teardown_tick(self) -> None:
        await self.bot.wait_until_ready()

    async def _capture_transcript(self, channel: discord.VoiceChannel) -> list[GroupTranscriptMessage]:
        messages: list[GroupTranscriptMessage] = []
        try:
            async for message in channel.history(limit=MAX_TRANSCRIPT_MESSAGES, oldest_first=True):
                if message.author.bot:
                    continue
                messages.append(
                    GroupTranscriptMessage(
                        author_discord_id=str(message.author.id),
                        author_name=str(message.author),
                        content=message.content[:4000],
                        created_at=message.created_at,
                        attachments=[attachment.url for attachment in message.attachments],
                    )
                )
        except discord.HTTPException:
            logger.warning("Failed to capture transcript for channel %s", channel.id, exc_info=True)
        return messages[:MAX_TRANSCRIPT_MESSAGES]

    async def _post_recap(self, guild: discord.Guild, live: LiveGroup) -> None:
        config = await self.bot.configs.get(guild.id)
        if config.log_channel_id is None:
            return
        channel = guild.get_channel(config.log_channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        duration_minutes = max(0, int((datetime.now(UTC) - live.started_at).total_seconds() // 60))
        names = [
            live.display_names.get(member_id, str(member_id))
            for member_id in sorted(live.minutes, key=lambda member_id: live.minutes[member_id], reverse=True)
        ]
        line = build_recap_line(
            display_name=live.display_name, duration_minutes=duration_minutes, participant_names=names
        )
        with contextlib.suppress(discord.Forbidden, discord.HTTPException):
            await channel.send(line, allowed_mentions=discord.AllowedMentions.none())

    async def _teardown(self, guild: discord.Guild, live: LiveGroup, *, reason: GroupEndReason) -> None:
        async with live.lock:
            if live.session_id not in self._groups:
                return
            channel = guild.get_channel(live.channel_id)
            transcript = await self._capture_transcript(channel) if isinstance(channel, discord.VoiceChannel) else []
            if reason in {GroupEndReason.EMPTY, GroupEndReason.UNJOINED} and _has_people(channel):
                return  # someone joined while the check or the transcript capture ran; keep the channel
            try:
                await self.bot.api.close_group(
                    guild.id,
                    live.session_id,
                    GroupSessionClose(end_reason=reason, presence=build_presence_report(live), transcript=transcript),
                )
            except APIError:
                logger.warning("Failed to close group session %s; will retry next tick", live.session_id, exc_info=True)
                return
            del self._groups[live.session_id]
            await self._delete_card_message(guild, live.card_message_id)
            if live.ever_joined:
                await self._post_recap(guild, live)
            if isinstance(channel, discord.VoiceChannel):
                with contextlib.suppress(discord.NotFound, discord.Forbidden):
                    await channel.delete(reason=f"LFG teardown ({reason.value})")
            if live.pug_role_id is not None:
                await self._teardown_pug_extras(guild, live)

    async def _teardown_pug_extras(self, guild: discord.Guild, live: LiveGroup) -> None:
        """Delete a PUG group's invite and its temporary per-group role, after the normal teardown steps."""
        if live.invite_code is not None:
            with contextlib.suppress(discord.HTTPException):
                await self.bot.http.delete_invite(live.invite_code, reason="LFG: PUG group ended")
        role = guild.get_role(live.pug_role_id) if live.pug_role_id is not None else None
        if role is not None:
            with contextlib.suppress(discord.HTTPException):
                await role.delete(reason="LFG: PUG group ended")

    # --- Daily transcript purge --------------------------------------------------------------------------------

    @tasks.loop(hours=DAILY_PURGE_INTERVAL_HOURS)
    async def _daily_purge(self) -> None:
        for guild in list(self.bot.guilds):
            try:
                await self.bot.api.purge_group_transcripts(guild.id)
            except APIError:
                logger.warning("Failed to purge group transcripts for guild %s", guild.id, exc_info=True)

    @_daily_purge.before_loop
    async def _before_daily_purge(self) -> None:
        await self.bot.wait_until_ready()

    # --- PUG sweep -----------------------------------------------------------------------------------------

    @tasks.loop(seconds=PUG_SWEEP_INTERVAL_SECONDS)
    async def _pug_sweep(self) -> None:
        now = datetime.now(UTC)
        for guild in list(self.bot.guilds):
            # Recover live groups first, so a restart never mistakes a PUG joiner in a live group for an orphan; a
            # failed recovery leaves the guild unrecovered, and the sweep waits for the next tick.
            await self._ensure_recovered(guild)
            if guild.id not in self._recovered_guild_ids:
                continue
            try:
                config = await self.bot.configs.get(guild.id)
            except APIError:
                # Must not escape: an unhandled exception stops a tasks.loop for good. Retried next tick.
                logger.warning("PUG sweep skipped for guild %s", guild.id, exc_info=True)
                continue
            if config.pug_role_id is None:
                continue
            role = guild.get_role(config.pug_role_id)
            if role is None:
                continue
            group_pug_role_ids = {
                live.pug_role_id
                for live in self._groups.values()
                if live.guild_id == guild.id and live.pug_role_id is not None
            }
            for member in list(role.members):
                await self._sweep_pug_member(guild, member, config, group_pug_role_ids, now)

    @_pug_sweep.before_loop
    async def _before_pug_sweep(self) -> None:
        await self.bot.wait_until_ready()

    async def _sweep_pug_member(
        self,
        guild: discord.Guild,
        member: discord.Member,
        config: GuildConfigSchema,
        group_pug_role_ids: set[int],
        now: datetime,
    ) -> None:
        member_role_ids = {role.id for role in member.roles}
        decision = pug_sweep_decision(
            member_role_ids=member_role_ids,
            assignable_role_ids={role.id for role in member.roles if role.is_assignable()},
            linked_role_id=config.linked_role_id,
            roster_role_id=config.roster_role_id,
            pug_role_id=cast("int", config.pug_role_id),
            gate_role_id=config.gate_role_id,
            group_pug_role_ids=group_pug_role_ids,
            orphaned_since=self._pug_orphan_since.get(member.id),
            now=now,
        )
        if member_role_ids & group_pug_role_ids or (
            (config.linked_role_id is not None and config.linked_role_id in member_role_ids)
            or (config.roster_role_id is not None and config.roster_role_id in member_role_ids)
        ):
            self._pug_orphan_since.pop(member.id, None)
        else:
            self._pug_orphan_since.setdefault(member.id, now)
        if decision == "strip":
            pug_role = guild.get_role(cast("int", config.pug_role_id))
            if pug_role is not None:
                with contextlib.suppress(discord.HTTPException):
                    await member.remove_roles(pug_role, reason="LFG: confirmed, PUG role no longer needed")
        elif decision == "kick":
            try:
                await member.kick(reason="LFG: PUG group ended without signing the ledger")
            except discord.Forbidden:
                if member.id not in self._pug_kick_forbidden_warned:
                    logger.warning(
                        "Missing permission to kick %s for an ended PUG group in guild %s", member.id, guild.id
                    )
                    self._pug_kick_forbidden_warned.add(member.id)
                return
            except discord.HTTPException:
                logger.warning("Failed to kick PUG joiner %s in guild %s", member.id, guild.id, exc_info=True)
                return
            self._pug_orphan_since.pop(member.id, None)

    # --- /lfg settings -----------------------------------------------------------------------------------------

    @lfg_group.command(name="channel", description="Set the channel group voice channels spawn next to.")
    @app_commands.describe(channel="Text channel for the LFG panel; must be inside a category.")
    async def lfg_channel(self, interaction: discord.Interaction[SideQuestBot], channel: discord.TextChannel) -> None:
        """Make ``channel`` bot-only, save it, and post/edit the panel."""
        if interaction.guild is None:
            return
        if channel.category is None:
            await interaction.response.send_message("That channel needs to be inside a category.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        overwrite = channel.overwrites_for(guild.default_role)
        overwrite.update(
            send_messages=False, create_public_threads=False, create_private_threads=False, add_reactions=False
        )
        warning = ""
        try:
            await channel.set_permissions(guild.default_role, overwrite=overwrite, reason="LFG panel channel")
        except discord.Forbidden:
            warning = " I lack Manage Roles to lock it to bot-only; lock it by hand."
        await self.bot.configs.update(guild.id, GuildConfigUpdate(lfg_channel_id=channel.id))
        await self._ensure_panel(guild, channel)
        await interaction.followup.send(f"Group voice panel set to {channel.mention}.{warning}", ephemeral=True)

    @lfg_group.command(name="pingrole", description="Set or clear the opt-in ping role for a group kind.")
    @app_commands.describe(kind="Group kind.", role="Role to ping when someone opts in; omit to clear.")
    @app_commands.choices(kind=_KIND_CHOICES)
    async def lfg_pingrole(
        self,
        interaction: discord.Interaction[SideQuestBot],
        kind: app_commands.Choice[str],
        role: discord.Role | None = None,
    ) -> None:
        """Update ``GuildConfig.lfg_ping_role_ids`` for ``kind``."""
        if interaction.guild is None:
            return
        guild = interaction.guild
        config = await self.bot.configs.get(guild.id)
        ping_role_ids = dict(config.lfg_ping_role_ids)
        if role is None:
            ping_role_ids.pop(kind.value, None)
            message = f"{kind.name} pings cleared."
            warning = ""
        else:
            ping_role_ids[kind.value] = role.id
            message = f"{kind.name} pings will ping {role.mention}."
            warning = (
                " I lack Mention Everyone and that role isn't mentionable, so pings for it may be silent."
                if not role.mentionable and not guild.me.guild_permissions.mention_everyone
                else ""
            )
        await self.bot.configs.update(guild.id, GuildConfigUpdate(lfg_ping_role_ids=ping_role_ids))
        await interaction.response.send_message(message + warning, ephemeral=True)

    @lfg_group.command(name="pugrole", description="Set (or clear) the role PUG invites grant to outside players.")
    @app_commands.describe(role="Role every PUG group invite grants; omit to disable PUG groups.")
    async def lfg_pugrole(
        self, interaction: discord.Interaction[SideQuestBot], role: discord.Role | None = None
    ) -> None:
        """Update ``GuildConfig.pug_role_id`` after checking the invoker may configure it and the bot may grant it.

        Replies with the channels ``role`` can currently see, so the admin can tell at a glance whether it's hidden
        down to #sign-the-ledger as intended before sharing any PUG invite links.
        """
        guild = interaction.guild
        if guild is None or not isinstance(interaction.user, discord.Member):
            return
        if role is not None:
            reason = autorole_block_reason(interaction.user, role)
            if reason is not None:
                await interaction.response.send_message(reason, ephemeral=True)
                return
        await self.bot.configs.update(guild.id, GuildConfigUpdate(pug_role_id=role.id if role else None))
        if role is None:
            await interaction.response.send_message("PUG groups disabled.", ephemeral=True)
            return
        visible = [
            channel.mention
            for channel in guild.channels
            if isinstance(channel, discord.TextChannel | discord.VoiceChannel)
            and channel.permissions_for(role).view_channel
        ]
        listing = ", ".join(visible) if visible else "nothing (fully hidden)"
        await interaction.response.send_message(
            f"PUG invites will grant {role.mention}. It can currently see: {listing}. Hide everything but "
            "#sign-the-ledger so outside players can't wander the server.",
            ephemeral=True,
        )

    @lfg_group.command(name="status", description="Show the LFG panel channel, ping roles, and live groups.")
    async def lfg_status(self, interaction: discord.Interaction[SideQuestBot]) -> None:
        """Ephemeral summary of the current LFG configuration and live groups."""
        if interaction.guild is None:
            return
        guild = interaction.guild
        config = await self.bot.configs.get(guild.id)
        await self._ensure_recovered(guild)
        channel = guild.get_channel(config.lfg_channel_id) if config.lfg_channel_id else None
        lines = [f"Panel: {channel.mention if isinstance(channel, discord.TextChannel) else 'not set'}"]
        if config.lfg_ping_role_ids:
            role_lines = []
            for kind_value, role_id in config.lfg_ping_role_ids.items():
                role = guild.get_role(role_id)
                label = GROUP_KIND_LABELS.get(EventKind(kind_value), kind_value)
                role_lines.append(f"{label}: {role.mention if role else role_id}")
            lines.append("Ping roles: " + ", ".join(role_lines))
        else:
            lines.append("Ping roles: none set")
        if config.pug_role_id is not None:
            pug_role = guild.get_role(config.pug_role_id)
            lines.append(f"PUG role: {pug_role.mention if pug_role else config.pug_role_id}")
        else:
            lines.append("PUG role: not set (PUG group button disabled)")
        live_here = [live for live in self._groups.values() if live.guild_id == guild.id]
        if live_here:
            lines.append(f"Live groups ({len(live_here)}):")
            lines += [
                f"- {live.display_name} ({GROUP_KIND_LABELS[live.kind]}) by <@{live.owner_id}>" for live in live_here
            ]
        else:
            lines.append("No live groups.")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)


async def setup(bot: SideQuestBot) -> None:
    """Register the persistent buttons and the cog."""
    bot.add_dynamic_items(SpawnButton, OwnerControlButton, OpenVoiceButton, PugButton)
    await bot.add_cog(GroupFinderCog(bot))
