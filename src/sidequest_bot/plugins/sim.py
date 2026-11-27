"""Gear-swap sims: a "sim" forum post names a character and up to six items, the bot queues a sim through the API.

A member starts a post in the forum channel named ``"sim"`` (any category). The starter message's first line is
the character's name and surname (``Nox Zul``); every following line is either an item to try (a Wowhead link, a
numeric item id, or an item name, optionally prefixed ``slot: `` and suffixed `` +Enchant Name``) or one of the
option lines documented in :data:`USAGE` (``targets:``, ``mobs:``, ``duration:``, ``spec:``, ``talents:``,
``wearing:``). The bot never resolves item text itself — the API's character/item/spec validation is the single
source of truth, and its plain-English 400/429 ``detail`` is relayed verbatim.

The sim site has no "what's my linked character" route yet (only an event-scoped signup lookup), so a post that
omits the character line always gets the usage reply rather than guessing; the first agent to add a generic
by-Discord-id character lookup should wire it in here as ``default_character``.

The bot replies "Simming…" right away, then polls ``GET .../sims/{id}`` every 5s for up to 5 minutes, editing that
reply with the queue position and, once done, the first chunk of the results (further chunks, if the tables don't
fit in one 2000-char message, post as new thread messages). A later message from the thread's owner re-parses and
re-runs the sim, cancelling any poll still in flight for that thread. Errors the API reports (unknown character,
ambiguous item, unsupported spec, the 429 queue limit) are replied in the thread verbatim.

The forum's own "Post Guidelines" (its channel topic, shown to posters starting a new thread) are set to
:data:`USAGE` once, the first time the bot finds the forum with no topic set; an officer who customizes it afterwards
is never overwritten.
"""

import asyncio
import contextlib
import logging
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import discord
from discord.ext import commands, tasks

from sidequest_bot.api_client import (
    MAX_SIM_SWAPS,
    MAX_SIM_WEARING,
    SIM_SLOTS,
    APIError,
    SimChange,
    SimComparison,
    SimItem,
    SimRequest,
    SimResult,
    SimRow,
)
from sidequest_bot.bot import SideQuestBot  # noqa: TC001  # discord.py evaluates app-command annotations at runtime

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

FORUM_NAME = "sim"
SWEEP_INTERVAL_HOURS = 6
POLL_INTERVAL_SECONDS = 5
POLL_TIMEOUT_SECONDS = 300
STARTER_RETRY_SECONDS = 2
DISCORD_MESSAGE_LIMIT = 2000

USAGE = (
    "**Sim a gear swap**\n"
    "First line: your character's name and surname. Then one item to try per line (1-6):\n"
    "```\n"
    "Legs Behindhead\n"
    "https://www.wowhead.com/forever/item=22332\n"
    "finger2: Seal of Rivendare\n"
    "Royal Tribunal Cloak\n"
    "```\n"
    "An item is a Wowhead link, an item id, or its name. Put `slot: ` in front to choose the slot (a ring or "
    "trinket without one is tried in both), and ` +Enchant Name` after to enchant it.\n"
    "Optional lines:\n"
    "- `wearing: chest: Plate of the Shaman King`: gear you wear now that Battle.net doesn't show yet (it updates "
    "when you log out). Repeat for each piece.\n"
    "- `talents: <wowhead talent string>`: your talents; without it the sim uses a default build for your spec.\n"
    "- `targets: 1, 4` · `mobs: humanoid, undead` · `duration: 120` · `spec: feralbear` (druids: feralcat or "
    "feralbear)\n"
    "The sim models level 60 (55+ is close enough). Healers, enhancement shamans and warlocks can't be simmed yet.\n"
    "Post again in your thread to re-run with new text."
)

_OPTION_KEYS = ("targets", "mobs", "duration", "spec", "talents", "wearing")
_OPTION_RE = re.compile(rf"^(?P<key>{'|'.join(_OPTION_KEYS)})\s*:\s*(?P<value>.*)$", re.IGNORECASE)
_SLOT_PATTERN = "|".join(re.escape(slot) for slot in SIM_SLOTS)
_SLOT_RE = re.compile(rf"^(?P<slot>{_SLOT_PATTERN})\s*:\s*(?P<rest>.*)$", re.IGNORECASE)
_ENCHANT_RE = re.compile(r"^(?P<item>.+?)\s\+(?P<enchant>\S.*)$")

_ROLE_METRICS: dict[str, list[tuple[str, str]]] = {
    "tank": [("tps", "Δ Threat"), ("dtps", "Δ Dmg Taken")],
    "healer": [("hps", "Δ Healing")],
    "dps": [("dps", "Δ DPS")],
}


@dataclass
class ParsedSim:
    """A sim post's text, resolved into the shape :class:`SimRequest` needs (minus ``requested_by``)."""

    character: str
    swaps: list[SimItem]
    wearing: list[SimItem]
    spec: str | None
    talents: str | None
    targets: list[int]
    mobs: list[str]
    duration: int


def parse_item_line(line: str) -> SimItem | str:
    """One item line into a :class:`SimItem`, or a plain-English error if it's empty after stripping prefixes."""
    rest = line
    slot: str | None = None
    slot_match = _SLOT_RE.match(rest)
    if slot_match:
        slot = slot_match.group("slot").lower()
        rest = slot_match.group("rest").strip()
    enchant: str | None = None
    enchant_match = _ENCHANT_RE.match(rest)
    if enchant_match:
        rest = enchant_match.group("item").strip()
        enchant = enchant_match.group("enchant").strip()
    item = rest.strip()
    if not item:
        return f"Couldn't find an item on this line: `{line}`"
    return SimItem(item=item, slot=slot, enchant=enchant)


@dataclass
class _Draft:
    """Mutable accumulator for :func:`parse_sim_post`, converted to a :class:`ParsedSim` once parsing succeeds."""

    swaps: list[SimItem] = field(default_factory=list)
    wearing: list[SimItem] = field(default_factory=list)
    spec: str | None = None
    talents: str | None = None
    targets: list[int] = field(default_factory=lambda: [1, 4])
    mobs: list[str] = field(default_factory=lambda: ["humanoid"])
    duration: int = 90


def _apply_option(draft: _Draft, key: str, value: str) -> str | None:
    """Apply one option line's ``key``/``value`` to ``draft``; returns an error reply, or ``None`` on success."""
    if key == "targets":
        try:
            draft.targets = [int(part.strip()) for part in value.split(",") if part.strip()]
        except ValueError:
            return f"`targets:` needs numbers, like `targets: 1, 4` (got `{value}`)."
    elif key == "mobs":
        draft.mobs = [part.strip().lower() for part in value.split(",") if part.strip()]
    elif key == "duration":
        try:
            draft.duration = int(value)
        except ValueError:
            return f"`duration:` needs a number of seconds (got `{value}`)."
    elif key == "spec":
        draft.spec = value or None
    elif key == "talents":
        draft.talents = value or None
    elif key == "wearing":
        parsed_wearing = parse_item_line(value)
        if isinstance(parsed_wearing, str):
            return parsed_wearing
        draft.wearing.append(parsed_wearing)
    return None


def _validate_draft(draft: _Draft) -> str | None:
    """A plain-English error if ``draft`` can't become a sim request, else ``None``."""
    if not draft.swaps:
        return f"List at least one item to try.\n\n{USAGE}"
    if len(draft.swaps) > MAX_SIM_SWAPS:
        return f"Up to {MAX_SIM_SWAPS} items per sim; this post has {len(draft.swaps)}."
    if len(draft.wearing) > MAX_SIM_WEARING:
        return f"Up to {MAX_SIM_WEARING} `wearing:` corrections; this post has {len(draft.wearing)}."
    return None


def parse_sim_post(content: str, *, default_character: str | None) -> ParsedSim | str:
    """``content`` (a forum starter post or thread-owner reply) into a :class:`ParsedSim`, or an error reply."""
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    if not lines:
        return USAGE

    character = default_character
    start = 0
    if _OPTION_RE.match(lines[0]) is None:
        character = lines[0]
        start = 1
    if character is None:
        return f"Name a character on the first line (like `Nox Zul`).\n\n{USAGE}"

    draft = _Draft()
    for line in lines[start:]:
        option_match = _OPTION_RE.match(line)
        if option_match is None:
            parsed_item = parse_item_line(line)
            if isinstance(parsed_item, str):
                return parsed_item
            draft.swaps.append(parsed_item)
            continue
        error = _apply_option(draft, option_match.group("key").lower(), option_match.group("value").strip())
        if error is not None:
            return error

    invalid = _validate_draft(draft)
    if invalid is not None:
        return invalid

    return ParsedSim(
        character=character,
        swaps=draft.swaps,
        wearing=draft.wearing,
        spec=draft.spec,
        talents=draft.talents,
        targets=draft.targets,
        mobs=draft.mobs,
        duration=draft.duration,
    )


def build_request(parsed: ParsedSim, *, requested_by: int | None) -> SimRequest:
    """``parsed`` plus the requesting Discord user id, as the API's ``SimRequest`` body."""
    return SimRequest(
        character=parsed.character,
        swaps=parsed.swaps,
        wearing=parsed.wearing,
        spec=parsed.spec,
        talents=parsed.talents,
        targets=parsed.targets,
        mobs=parsed.mobs,
        duration=parsed.duration,
        requested_by=str(requested_by) if requested_by is not None else None,
    )


def format_change(change: SimChange) -> str:
    """One slot's before/after item, e.g. ``**chest**: Breastplate of Valor → Breastplate of Annihilation``."""
    from_name = change.from_item.name if change.from_item else "—"
    return f"**{change.slot}**: {from_name} → {change.to_item.name}"


def _delta_cell(row: SimRow, key: str) -> str:
    value = getattr(row.delta_pct, key)
    text = "—" if value is None else f"{value:+.1f}%"
    return f"{text} ~" if key in row.noise else text


def build_comparison_table(comparison: SimComparison, role: str) -> str:
    """``comparison``'s rows as a monospace table (Discord doesn't render markdown tables), columns by ``role``.

    A ``~`` after a change marks it as noise: within two standard errors, so no real difference.
    """
    metrics = _ROLE_METRICS.get(role, _ROLE_METRICS["dps"])
    headers = ["Targets", "Mob", *(label for _, label in metrics)]
    rows = [[str(row.targets), row.mob, *(_delta_cell(row, key) for key, _ in metrics)] for row in comparison.rows]
    widths = [max(len(cells[i]) for cells in [headers, *rows]) for i in range(len(headers))]
    lines = [
        "  ".join(cell.ljust(width) for cell, width in zip(cells, widths, strict=True)).rstrip()
        for cells in [headers, *rows]
    ]
    legend = "\n~ = noise (within 2 standard errors)" if any(row.noise for row in comparison.rows) else ""
    return "```\n" + "\n".join(lines) + legend + "\n```"


def build_comparison_block(comparison: SimComparison, role: str, *, heading: str | None = None) -> str:
    """One comparison's optional heading, gear changes, and results table, as one message block."""
    changes = "\n".join(f"- {format_change(change)}" for change in comparison.changes)
    table = build_comparison_table(comparison, role)
    title = f"**{heading}**\n" if heading else ""
    return f"{title}{changes}\n{table}"


def build_footer(result: SimResult) -> str:
    """The subtext line(s): any notes (skipped enchants, etc.) plus iterations/duration/gear date."""
    gear_date = result.gear_fetched_at.strftime("%Y-%m-%d") if result.gear_fetched_at else "unknown"
    lines = [f"-# {note}" for note in result.notes]
    lines.append(f"-# {result.iterations} iterations · {result.duration}s fights · gear as of {gear_date}")
    return "\n".join(lines)


def _pack(parts: Sequence[str], limit: int = DISCORD_MESSAGE_LIMIT) -> list[str]:
    """Greedily pack ``parts`` into blank-line-joined chunks no longer than ``limit`` characters."""
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for part in parts:
        added = len(part) + (2 if current else 0)
        if current and current_len + added > limit:
            chunks.append("\n\n".join(current))
            current = [part]
            current_len = len(part)
        else:
            current.append(part)
            current_len += added
    if current:
        chunks.append("\n\n".join(current))
    return chunks or [""]


def format_sim_results(result: SimResult) -> list[str]:
    """``result`` as one or more message bodies (each under Discord's 2000-char limit), in posting order.

    A ring or trinket posted without a slot comes back as one comparison per slot; those get "Option N" headings.
    """
    header = f"**{result.character}** — {result.class_name.title()} {result.spec.title()} ({result.role})"
    several = len(result.comparisons) > 1
    blocks = [
        build_comparison_block(comparison, result.role, heading=f"Option {i}" if several else None)
        for i, comparison in enumerate(result.comparisons, start=1)
    ]
    return _pack([header, *blocks, build_footer(result)])


class SimCog(commands.Cog):
    """Watches the ``sim`` forum, queues sims through the API, and posts their results tables."""

    def __init__(self, bot: SideQuestBot) -> None:
        self.bot = bot
        self._polls: dict[int, asyncio.Task[None]] = {}
        self._guideline_sweep.start()

    async def cog_unload(self) -> None:
        """Stop the guideline sweep and cancel any sims still being polled."""
        self._guideline_sweep.cancel()
        for task in self._polls.values():
            task.cancel()

    @staticmethod
    def _find_forum(guild: discord.Guild) -> discord.ForumChannel | None:
        return discord.utils.get(guild.forums, name=FORUM_NAME)

    @staticmethod
    def _is_sim_thread(thread: discord.Thread) -> bool:
        return isinstance(thread.parent, discord.ForumChannel) and thread.parent.name == FORUM_NAME

    @tasks.loop(hours=SWEEP_INTERVAL_HOURS)
    async def _guideline_sweep(self) -> None:
        """Set the sim forum's post guidelines to :data:`USAGE` the first time it's found with none set."""
        for guild in self.bot.guilds:
            forum = self._find_forum(guild)
            if forum is None or forum.topic:
                continue
            try:
                await forum.edit(topic=USAGE)
            except discord.HTTPException:
                logger.warning("Couldn't set sim forum guidelines in guild %s", guild.id, exc_info=True)

    @_guideline_sweep.before_loop
    async def _before_sweep(self) -> None:
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_thread_create(self, thread: discord.Thread) -> None:
        """Parse a new sim post's starter message and queue it."""
        if not self._is_sim_thread(thread):
            return
        starter = thread.starter_message
        for attempt in range(2):  # the starter post can lag the thread-create event by a moment
            if starter is not None:
                break
            try:
                starter = await thread.fetch_message(thread.id)
            except discord.NotFound:
                if attempt == 0:
                    await asyncio.sleep(STARTER_RETRY_SECONDS)
            except discord.HTTPException:
                break
        if starter is None:
            logger.warning("Couldn't read the starter post for sim thread %s", thread.id)
            return
        await self._handle(thread, starter.content, thread.owner_id)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Re-run a sim thread when its owner posts a follow-up message."""
        if message.author.bot or not isinstance(message.channel, discord.Thread):
            return
        thread = message.channel
        if not self._is_sim_thread(thread) or message.id == thread.id or message.author.id != thread.owner_id:
            return
        await self._handle(thread, message.content, thread.owner_id)

    async def _handle(self, thread: discord.Thread, content: str, owner_id: int | None) -> None:
        parsed = parse_sim_post(content, default_character=None)
        if isinstance(parsed, str):
            await thread.send(parsed)
            return
        request = build_request(parsed, requested_by=owner_id)
        reply = await thread.send("Simming…")
        previous = self._polls.get(thread.id)
        if previous is not None and not previous.done():
            previous.cancel()
        task = asyncio.create_task(self._poll(thread, reply, request))
        self._polls[thread.id] = task
        task.add_done_callback(lambda _task, thread_id=thread.id: self._polls.pop(thread_id, None))

    async def _poll(self, thread: discord.Thread, reply: discord.Message, request: SimRequest) -> None:
        guild_id = thread.guild.id
        try:
            job = await self.bot.api.create_sim_job(guild_id, request)
        except APIError as exc:
            await reply.edit(content=f"Couldn't queue that sim: {exc.detail}")
            return

        deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
        while True:
            if job.status == "done" and job.result is not None:
                await self._post_results(thread, reply, job.result)
                return
            if job.status == "failed":
                await reply.edit(content=f"That sim failed: {job.error or 'unknown error'}")
                return
            if time.monotonic() >= deadline:
                await reply.edit(content="That sim is taking too long; try again in a bit.")
                return
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            try:
                job = await self.bot.api.get_sim_job(guild_id, job.id)
            except APIError as exc:
                await reply.edit(content=f"Lost track of that sim: {exc.detail}")
                return
            position = f" (position {job.position} in queue)" if job.position else ""
            with contextlib.suppress(discord.HTTPException):
                await reply.edit(content=f"Simming…{position}")

    @staticmethod
    async def _post_results(thread: discord.Thread, reply: discord.Message, result: SimResult) -> None:
        chunks = format_sim_results(result)
        await reply.edit(content=chunks[0])
        for chunk in chunks[1:]:
            await thread.send(chunk)


async def setup(bot: SideQuestBot) -> None:
    """Register the cog."""
    await bot.add_cog(SimCog(bot))
