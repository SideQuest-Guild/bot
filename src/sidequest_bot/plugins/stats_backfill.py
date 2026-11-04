"""`/stats backfill`: a one-time admin command that reads past message counts from Discord channel history.

Live tracking of chat messages happens on the API side (``MemberLevelService.award_xp``); this command only backfills
history from before that started. It walks every readable text channel, voice-channel text chat, and thread
(active and archived public threads of text and forum channels), counts XP-eligible messages per member per UTC
hour the same way chat XP does, and pushes the counts to the API in chunks. Idempotent and safe to re-run: the API
keeps ``max(existing, reported)`` per bucket. Channels the bot can't read are skipped, not fatal.
"""

import contextlib
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from sidequest_bot.api_client import APIError, NotFoundError
from sidequest_bot.bot import SideQuestBot  # noqa: TC001  # discord.py evaluates app-command annotations at runtime
from sidequest_bot.plugins.leveling import message_channel_ids
from sidequest_common.schemas import MessageBackfill, MessageBackfillBucket

if TYPE_CHECKING:
    from sidequest_common.schemas import GuildConfigSchema

logger = logging.getLogger(__name__)

DEFAULT_BACKFILL_DAYS = 365
MAX_BACKFILL_DAYS = 3650
BACKFILL_CHUNK_SIZE = 5000
"""Max buckets per ``PUT .../activity/messages`` call, matching ``MessageBackfill``'s limit."""

type BackfillTarget = discord.TextChannel | discord.VoiceChannel | discord.StageChannel | discord.Thread


def is_backfill_eligible(message: discord.Message, config: GuildConfigSchema) -> bool:
    """Whether ``message`` counts toward the backfill: the same rule as chat XP, minus ``leveling_enabled``.

    Backfilled counts feed server stats, which track activity even in guilds with leveling turned off.
    """
    if message.author.bot or message.webhook_id is not None:
        return False
    if message.is_system():
        return False
    if message_channel_ids(message) & set(config.no_xp_channel_ids):
        return False
    if isinstance(message.author, discord.Member):
        author_role_ids = {role.id for role in message.author.roles}
        if author_role_ids & set(config.no_xp_role_ids):
            return False
    return True


async def _channel_threads(channel: discord.TextChannel | discord.ForumChannel) -> list[discord.Thread]:
    """Active and archived public threads of ``channel``; archived ones are skipped, not fatal, if unreadable."""
    threads = list(channel.threads)
    with contextlib.suppress(discord.Forbidden):
        if isinstance(channel, discord.TextChannel):
            threads.extend([thread async for thread in channel.archived_threads(private=False, limit=None)])
        else:
            threads.extend([thread async for thread in channel.archived_threads(limit=None)])
    return threads


async def backfill_targets(guild: discord.Guild) -> list[BackfillTarget]:
    """Every text channel, voice-channel text chat, and thread (active + archived public) worth scanning."""
    targets: list[BackfillTarget] = [*guild.text_channels, *guild.voice_channels, *guild.stage_channels]
    for channel in (*guild.text_channels, *guild.forums):
        targets.extend(await _channel_threads(channel))
    return targets


@dataclass
class BackfillResult:
    """Totals from one backfill scan, ready to chunk and send."""

    counts: dict[tuple[int, datetime], int] = field(default_factory=dict)
    channels_scanned: int = 0
    messages_counted: int = 0


async def collect_message_counts(guild: discord.Guild, config: GuildConfigSchema, *, since: datetime) -> BackfillResult:
    """Count XP-eligible messages per (author id, UTC hour) across every readable channel since ``since``."""
    counts: dict[tuple[int, datetime], int] = defaultdict(int)
    result = BackfillResult(counts=counts)
    for channel in await backfill_targets(guild):
        result.channels_scanned += 1
        try:
            async for message in channel.history(after=since, limit=None, oldest_first=True):
                if not is_backfill_eligible(message, config):
                    continue
                hour = message.created_at.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
                counts[message.author.id, hour] += 1
                result.messages_counted += 1
        except discord.Forbidden:
            continue
    return result


def chunk_buckets(counts: dict[tuple[int, datetime], int]) -> list[list[MessageBackfillBucket]]:
    """``counts`` as ``MessageBackfillBucket`` rows, split into chunks of at most ``BACKFILL_CHUNK_SIZE``."""
    buckets = [
        MessageBackfillBucket(user_id=user_id, hour_start=hour, messages=messages)
        for (user_id, hour), messages in counts.items()
    ]
    return [buckets[i : i + BACKFILL_CHUNK_SIZE] for i in range(0, len(buckets), BACKFILL_CHUNK_SIZE)]


class StatsBackfillCog(commands.Cog):
    """The ``/stats backfill`` admin command: one scan at a time per guild, with progress posted ephemerally."""

    stats_group = app_commands.Group(
        name="stats",
        description="Server statistics.",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    def __init__(self, bot: SideQuestBot) -> None:
        self.bot = bot
        self._running: set[int] = set()

    @stats_group.command(name="backfill", description="Read past message counts from channel history for server stats.")
    @app_commands.describe(days="How many days of history to scan (default 365)")
    async def backfill(
        self,
        interaction: discord.Interaction[SideQuestBot],
        days: app_commands.Range[int, 1, MAX_BACKFILL_DAYS] = DEFAULT_BACKFILL_DAYS,
    ) -> None:
        """Acknowledge ephemerally, then scan and push message counts in the background."""
        guild = interaction.guild
        if guild is None:
            return
        if guild.id in self._running:
            await interaction.response.send_message("A backfill is already running for this server.", ephemeral=True)
            return
        self._running.add(guild.id)
        await interaction.response.send_message(f"Scanning the last {days} day(s) of message history…", ephemeral=True)
        self.bot.loop.create_task(self._run(interaction, guild, days))

    async def _run(self, interaction: discord.Interaction[SideQuestBot], guild: discord.Guild, days: int) -> None:
        try:
            await self._backfill(interaction, guild, days)
        finally:
            self._running.discard(guild.id)

    async def _backfill(self, interaction: discord.Interaction[SideQuestBot], guild: discord.Guild, days: int) -> None:
        config = await self.bot.configs.get(guild.id)
        since = discord.utils.utcnow() - timedelta(days=days)
        result = await collect_message_counts(guild, config, since=since)
        await self._safe_edit(
            interaction,
            f"Scanned {result.channels_scanned} channel(s), counted {result.messages_counted} message(s); "
            "sending to the API…",
        )
        buckets_sent = 0
        for chunk in chunk_buckets(result.counts):
            try:
                response = await self.bot.api.backfill_messages(guild.id, MessageBackfill(buckets=chunk))
            except NotFoundError:
                await self._safe_edit(
                    interaction,
                    f"Scanned {result.channels_scanned} channel(s), counted {result.messages_counted} message(s), "
                    "but the stats API isn't deployed yet; try again later.",
                )
                return
            except APIError:
                logger.warning("Failed to push backfill buckets for guild %s", guild.id, exc_info=True)
                await self._safe_edit(
                    interaction,
                    f"Scanned {result.channels_scanned} channel(s), counted {result.messages_counted} message(s), "
                    "but couldn't reach the guild records; try again shortly.",
                )
                return
            buckets_sent += response.buckets
        await self._safe_edit(
            interaction,
            f"Backfill done: scanned {result.channels_scanned} channel(s), counted {result.messages_counted} "
            f"message(s), sent {buckets_sent} bucket(s) of activity.",
        )

    async def _safe_edit(self, interaction: discord.Interaction[SideQuestBot], content: str) -> None:
        try:
            await interaction.edit_original_response(content=content)
        except discord.HTTPException:
            logger.warning("Failed to edit backfill progress message for guild %s", interaction.guild_id, exc_info=True)


async def setup(bot: SideQuestBot) -> None:
    """Register the cog and its ``/stats`` command."""
    await bot.add_cog(StatsBackfillCog(bot))
