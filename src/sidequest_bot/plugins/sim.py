"""Gear-swap sims: messages in the "Gear sims" post of the ``#class-halls`` forum queue a sim through the API.

The bot keeps one post named :data:`POST_NAME` in the forum named :data:`FORUM_NAME` (the class halls), creating it
with :data:`USAGE` as its first message when it's missing and reopening it when Discord archives it. Every member
message in that post is a sim request: its first line is the character's name and surname (``Nox Zul``); every
following line is either an item to try (a Wowhead link, a numeric item id, or an item name, optionally prefixed
``slot: `` and suffixed `` +Enchant Name``) or one of the option lines documented in :data:`USAGE` (``targets:``,
``mobs:``, ``duration:``, ``spec:``, ``talents:``, ``wearing:``). The bot never resolves item text itself: the API's
character/item/spec validation is the single source of truth, and its plain-English 400/429 ``detail`` is relayed
verbatim.

The sim site has no "what's my linked character" route yet (only an event-scoped signup lookup), so a message that
omits the character line gets the usage reply rather than a guess; a by-Discord-id character lookup would plug in
here as ``default_character``.

The bot replies to the message with "Simming…" right away, then polls ``GET .../sims/{id}`` every 5s for up to 5
minutes, editing that reply with the queue position and, once done, the first chunk of the results (further chunks,
if the tables don't fit in one 2000-char message, follow as more replies). Errors the API reports (unknown
character, ambiguous item, unsupported spec, the 429 queue limit) are replied verbatim.
"""

import asyncio
import contextlib
import io
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
    SimResultItem,
    SimRow,
)
from sidequest_bot.bot import SideQuestBot  # noqa: TC001  # discord.py evaluates app-command annotations at runtime
from sidequest_bot.lib.sim_chart import Bar, Effect, MetricGroup, Panel, render_chart
from sidequest_bot.plugins.items import WOWHEAD_URL

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

FORUM_NAME = "class-halls"
POST_NAME = "Gear sims"
SWEEP_INTERVAL_HOURS = 6
POLL_INTERVAL_SECONDS = 5
POLL_TIMEOUT_SECONDS = 300
DISCORD_MESSAGE_LIMIT = 2000
CHART_FILENAME = "gear-sim.png"

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
    "Post a new message here to sim again."
)

_OPTION_KEYS = ("targets", "mobs", "duration", "spec", "talents", "wearing")
_OPTION_RE = re.compile(rf"^(?P<key>{'|'.join(_OPTION_KEYS)})\s*:\s*(?P<value>.*)$", re.IGNORECASE)
_SLOT_PATTERN = "|".join(re.escape(slot) for slot in SIM_SLOTS)
_SLOT_RE = re.compile(rf"^(?P<slot>{_SLOT_PATTERN})\s*:\s*(?P<rest>.*)$", re.IGNORECASE)
_ENCHANT_RE = re.compile(r"^(?P<item>.+?)\s\+(?P<enchant>\S.*)$")


@dataclass(frozen=True)
class _Metric:
    """A role's headline number: its result key, its name in sentences, and which way is good."""

    key: str
    noun: str
    higher_is_better: bool


_ROLE_METRICS: dict[str, list[_Metric]] = {
    "tank": [_Metric("tps", "threat", higher_is_better=True), _Metric("dtps", "damage taken", higher_is_better=False)],
    "healer": [_Metric("hps", "healing", higher_is_better=True)],
    "dps": [_Metric("dps", "damage", higher_is_better=True)],
}
SLOT_LABELS: dict[str, str] = {
    "head": "Head",
    "neck": "Neck",
    "shoulder": "Shoulders",
    "back": "Back",
    "chest": "Chest",
    "wrist": "Wrists",
    "hands": "Hands",
    "waist": "Waist",
    "legs": "Legs",
    "feet": "Feet",
    "finger1": "Ring 1",
    "finger2": "Ring 2",
    "trinket1": "Trinket 1",
    "trinket2": "Trinket 2",
    "main_hand": "Main hand",
    "off_hand": "Off hand",
    "ranged": "Ranged",
}
_EFFECT_DOT: dict[Effect, str] = {"better": "🟢", "worse": "🔴", "same": "⚪"}


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


def _item_link(item: SimResultItem) -> str:
    """The item's name linked to its Wowhead page; ``<>`` keeps Discord from adding a preview card per link."""
    return f"[{item.name}](<{WOWHEAD_URL.format(item_id=item.id)}>)"


def format_change(change: SimChange) -> str:
    """One slot's before/after item, each linked to Wowhead: ``**Chest:** [Old](<url>) → [New](<url>)``."""
    from_name = _item_link(change.from_item) if change.from_item else "nothing"
    slot = SLOT_LABELS.get(change.slot, change.slot)
    return f"**{slot}:** {from_name} → {_item_link(change.to_item)}"


def _metrics(role: str) -> list[_Metric]:
    return _ROLE_METRICS.get(role, _ROLE_METRICS["dps"])


def _oriented(row: SimRow, metric: _Metric) -> float | None:
    """``metric``'s % change with the sign flipped where lower is better, so positive always means better."""
    value = getattr(row.delta_pct, metric.key)
    if value is None:
        return None
    return value if metric.higher_is_better else -value


def _effect(row: SimRow, metric: _Metric) -> Effect:
    value = _oriented(row, metric)
    if value is None or value == 0 or metric.key in row.noise:
        return "same"
    return "better" if value > 0 else "worse"


def _change_words(row: SimRow, metric: _Metric) -> str:
    """``2.5% more threat``, ``0.5% less damage taken``, or ``threat about the same`` (within noise)."""
    value = getattr(row.delta_pct, metric.key)
    if value is None or metric.key in row.noise or round(value, 1) == 0:
        return f"{metric.noun} about the same"
    return f"{abs(value):.1f}% {'more' if value > 0 else 'less'} {metric.noun}"


def _fight_label(row: SimRow, *, show_mob: bool) -> str:
    """``1 enemy`` / ``4 enemies``, plus the mob type when the sim ran more than one."""
    label = "1 enemy" if row.targets == 1 else f"{row.targets} enemies"
    return f"{label}, {row.mob}" if show_mob else label


def _fight_kind(row: SimRow) -> str:
    return "a boss" if row.targets == 1 else "packs"


def _names(comparison: SimComparison) -> tuple[str, str]:
    """``(new, old)`` for the verdict: the item names for a single swap, generic words for several."""
    if len(comparison.changes) == 1:
        change = comparison.changes[0]
        return change.to_item.name, change.from_item.name if change.from_item else "the empty slot"
    return "the new gear", "your current gear"


def _directions(effects: Sequence[Effect]) -> tuple[bool, bool]:
    return "better" in effects, "worse" in effects


def verdict(comparison: SimComparison, role: str) -> str:
    """A plain-English pick for ``comparison``: pick it, keep what you have, either, or pick it *if* something holds.

    Changes within the sim's margin of error count as "the same". A tank trade-off (more threat for more damage
    taken, or the reverse) and a boss-versus-packs split each get their own "pick X if" advice.
    """
    new, old = _names(comparison)
    metrics = _metrics(role)
    by_metric = {metric.key: [_effect(row, metric) for row in comparison.rows] for metric in metrics}
    everything = [effect for effects in by_metric.values() for effect in effects]
    gains, losses = _directions(everything)
    if not gains and not losses:
        return f"**Either is fine.** {new} and {old} come out the same in these fights."
    if not losses:
        better = [m for m in metrics if "better" in by_metric[m.key]]
        return f"**Pick {new}.** You get {' and '.join(_more_or_less(m) for m in better)}."
    if not gains:
        worse = [m for m in metrics if "worse" in by_metric[m.key]]
        return f"**Keep {old}.** {new} gives you {' and '.join(_more_or_less(m, worse=True) for m in worse)}."

    return (_tank_tradeoff(by_metric, new, old) if role == "tank" else None) or _boss_or_packs(
        comparison, metrics, new, old
    )


def _tank_tradeoff(by_metric: dict[str, list[Effect]], new: str, old: str) -> str | None:
    """The "pick X if" advice when one of threat and damage taken only improves and the other only gets worse."""
    threat = _directions(by_metric["tps"])
    damage = _directions(by_metric["dtps"])
    if threat == (True, False) and damage == (False, True):
        return (
            f"**Pick {new} if** you're losing threat and your healer keeps up; **keep {old}** for bosses that hit hard."
        )
    if threat == (False, True) and damage == (True, False):
        return f"**Pick {new} if** you're taking too much damage; **keep {old}** if you're losing threat."
    return None


def _boss_or_packs(comparison: SimComparison, metrics: Sequence[_Metric], new: str, old: str) -> str:
    """Bosses-versus-packs advice when one target count only gains and the rest only lose, else a close call."""
    scores = {row.targets: 0 for row in comparison.rows}
    for row in comparison.rows:
        for metric in metrics:
            effect = _effect(row, metric)
            scores[row.targets] += 1 if effect == "better" else -1 if effect == "worse" else 0
    single = scores.get(1, 0)
    packs = [score for targets, score in scores.items() if targets > 1]
    if 1 in scores and packs:
        if single > 0 and all(score < 0 for score in packs):
            return f"**Pick {new} for bosses** (one enemy); **keep {old} for packs**."
        if single < 0 and all(score > 0 for score in packs):
            return f"**Pick {new} for packs**; **keep {old} for bosses** (one enemy)."
    return f"**Close call.** {new} wins some fights and loses others; see the chart."


def _more_or_less(metric: _Metric, *, worse: bool = False) -> str:
    """``more threat`` / ``less damage taken`` (or the opposite with ``worse``)."""
    more = metric.higher_is_better != worse
    return f"{'more' if more else 'less'} {metric.noun}"


def _score(comparison: SimComparison, role: str) -> float:
    """Sum of every real (non-noise) change, oriented so higher is better; ranks options against each other."""
    total = 0.0
    for row in comparison.rows:
        for metric in _metrics(role):
            value = _oriented(row, metric)
            if value is not None and metric.key not in row.noise:
                total += value
    return total


def build_comparison_block(comparison: SimComparison, role: str, *, heading: str | None = None) -> str:
    """One option's heading, gear changes, verdict, and a plain-English line per fight."""
    metrics = _metrics(role)
    show_mob = len({row.mob for row in comparison.rows}) > 1
    lines = [f"**{heading}**"] if heading else []
    lines += [format_change(change) for change in comparison.changes]
    lines.append(verdict(comparison, role))
    for row in comparison.rows:
        kind = f" ({_fight_kind(row)})" if not show_mob else ""
        parts = " · ".join(f"{_EFFECT_DOT[_effect(row, m)]} {_change_words(row, m)}" for m in metrics)
        lines.append(f"**{_fight_label(row, show_mob=show_mob)}{kind}:** {parts}")
    return "\n".join(lines)


def build_footer(result: SimResult) -> str:
    """The subtext line(s): any notes (skipped enchants, etc.), what ⚪ means, then runs/fight length/gear date."""
    gear_date = result.gear_fetched_at.strftime("%Y-%m-%d") if result.gear_fetched_at else "unknown"
    lines = [f"-# {note}" for note in result.notes]
    if any(row.noise for comparison in result.comparisons for row in comparison.rows):
        lines.append("-# ⚪ = too small to tell apart from the sim's random variation")
    lines.append(f"-# Simmed {result.iterations} times per fight · {result.duration}s fights · gear as of {gear_date}")
    return "\n".join(lines)


def _split_long(part: str, limit: int) -> list[str]:
    """``part`` cut at line breaks into pieces no longer than ``limit`` (an option with many fights can overflow)."""
    if len(part) <= limit:
        return [part]
    pieces: list[str] = []
    current = ""
    for line in part.split("\n"):
        if current and len(current) + 1 + len(line) > limit:
            pieces.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    pieces.append(current)
    return pieces


def _pack(parts: Sequence[str], limit: int = DISCORD_MESSAGE_LIMIT) -> list[str]:
    """Greedily pack ``parts`` into blank-line-joined chunks no longer than ``limit`` characters."""
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for part in (piece for whole in parts for piece in _split_long(whole, limit)):
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


def _best_line(comparisons: Sequence[SimComparison], role: str) -> str:
    """Which option to take, e.g. ``Best: Seal of Rivendare in Ring 2 (instead of Rosewine Circle)``."""
    scores = [_score(comparison, role) for comparison in comparisons]
    best = max(range(len(comparisons)), key=scores.__getitem__)
    changes = comparisons[best].changes
    if len(changes) != 1:
        return f"**Best: Option {best + 1}**"
    change = changes[0]
    instead = f" (instead of {change.from_item.name})" if change.from_item else ""
    return f"**Best: {change.to_item.name} in {SLOT_LABELS.get(change.slot, change.slot)}**{instead}"


def format_sim_results(result: SimResult) -> list[str]:
    """``result`` as one or more message bodies (each under Discord's 2000-char limit), in posting order.

    A ring or trinket posted without a slot comes back as one comparison per slot; those get "Option N" headings
    and a line naming the best one.
    """
    header = f"**{result.character}** · {result.spec.title()} {result.class_name.title()} ({result.role})"
    comparisons = result.comparisons
    several = len(comparisons) > 1
    if several:
        header += f"\n{_best_line(comparisons, result.role)}"
    blocks = [
        build_comparison_block(comparison, result.role, heading=f"Option {i}" if several else None)
        for i, comparison in enumerate(comparisons, start=1)
    ]
    return _pack([header, *blocks, build_footer(result)])


def _chart_text(row: SimRow, metric: _Metric) -> str:
    value = getattr(row.delta_pct, metric.key)
    if value is None or metric.key in row.noise or round(value, 1) == 0:
        return "about the same"
    return f"{abs(value):.1f}% {'more' if value > 0 else 'less'}"


def chart_panels(result: SimResult) -> list[Panel]:
    """``result`` as chart panels: one per option, one group per metric, one bar per fight."""
    panels: list[Panel] = []
    several = len(result.comparisons) > 1
    for index, comparison in enumerate(result.comparisons, start=1):
        new, old = _names(comparison)
        title = f"{new} vs {old}"
        if several:
            title = f"Option {index}: {title}"
        show_mob = len({row.mob for row in comparison.rows}) > 1
        groups = [
            MetricGroup(
                title=metric.noun.capitalize(),
                bars=[
                    Bar(
                        label=_fight_label(row, show_mob=show_mob),
                        value=0.0 if _effect(row, metric) == "same" else _oriented(row, metric) or 0.0,
                        text=_chart_text(row, metric),
                        effect=_effect(row, metric),
                    )
                    for row in comparison.rows
                ],
            )
            for metric in _metrics(result.role)
        ]
        panels.append(Panel(title=title, groups=groups))
    return panels


class SimCog(commands.Cog):
    """Keeps the "Gear sims" post in the class halls, queues each message there as a sim, and replies with results."""

    def __init__(self, bot: SideQuestBot) -> None:
        self.bot = bot
        self._polls: set[asyncio.Task[None]] = set()
        self._post_sweep.start()

    async def cog_unload(self) -> None:
        """Stop the post sweep and cancel any sims still being polled."""
        self._post_sweep.cancel()
        for task in self._polls:
            task.cancel()

    @staticmethod
    def is_sim_post(channel: object) -> bool:
        """Whether ``channel`` is the "Gear sims" post in the class-halls forum."""
        return (
            isinstance(channel, discord.Thread)
            and channel.name == POST_NAME
            and isinstance(channel.parent, discord.ForumChannel)
            and channel.parent.name == FORUM_NAME
        )

    @staticmethod
    async def _find_post(forum: discord.ForumChannel) -> discord.Thread | None:
        post = discord.utils.get(forum.threads, name=POST_NAME)
        if post is not None:
            return post
        async for thread in forum.archived_threads(limit=None):
            if thread.name == POST_NAME:
                return thread
        return None

    @tasks.loop(hours=SWEEP_INTERVAL_HOURS)
    async def _post_sweep(self) -> None:
        """Create the "Gear sims" post with :data:`USAGE` where it's missing, and reopen it if it was archived."""
        for guild in self.bot.guilds:
            forum = discord.utils.get(guild.forums, name=FORUM_NAME)
            if forum is None:
                continue
            try:
                post = await self._find_post(forum)
                if post is None:
                    await forum.create_thread(name=POST_NAME, content=USAGE)
                elif post.archived:
                    await post.edit(archived=False)
            except discord.HTTPException:
                logger.warning("Couldn't keep the %s post in guild %s", POST_NAME, guild.id, exc_info=True)

    @_post_sweep.before_loop
    async def _before_sweep(self) -> None:
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Sim a member's message in the "Gear sims" post (the post's own first message is the usage text)."""
        if message.author.bot or not self.is_sim_post(message.channel) or message.id == message.channel.id:
            return
        parsed = parse_sim_post(message.content, default_character=None)
        if isinstance(parsed, str):
            await message.reply(parsed, mention_author=False)
            return
        reply = await message.reply("Simming…", mention_author=False)
        request = build_request(parsed, requested_by=message.author.id)
        task = asyncio.create_task(self._poll(message, reply, request))
        self._polls.add(task)
        task.add_done_callback(self._polls.discard)

    async def _poll(self, message: discord.Message, reply: discord.Message, request: SimRequest) -> None:
        guild_id = message.guild.id if message.guild else 0
        try:
            job = await self.bot.api.create_sim_job(guild_id, request)
        except APIError as exc:
            await reply.edit(content=f"Couldn't queue that sim: {exc.detail}")
            return

        deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
        while True:
            if job.status == "done" and job.result is not None:
                await self._post_results(message, reply, job.result)
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
    async def _post_results(message: discord.Message, reply: discord.Message, result: SimResult) -> None:
        chunks = format_sim_results(result)
        files: list[discord.File] = []
        if result.comparisons:
            png = await asyncio.to_thread(render_chart, chart_panels(result))
            files.append(discord.File(io.BytesIO(png), filename=CHART_FILENAME))
        await reply.edit(content=chunks[0], attachments=files)
        for chunk in chunks[1:]:
            await message.reply(chunk, mention_author=False)


async def setup(bot: SideQuestBot) -> None:
    """Register the cog."""
    await bot.add_cog(SimCog(bot))
