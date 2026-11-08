"""Posts council loot decisions and voids to #loot-log, plus the ``/lootlog channel`` admin command.

A loop checks every few minutes: if a channel is set, it fetches pending council log entries (decisions and voids
queued by the website's loot council import) and posts one embed per entry, acking each after a successful send so
the website never resends it. If no channel is set, the loop does nothing and the rows wait.
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

from sidequest_bot.api_client import APIError, NotFoundError
from sidequest_bot.bot import SideQuestBot  # noqa: TC001  # discord.py evaluates app-command annotations at runtime
from sidequest_bot.lib.embeds import Colors, make_embed, truncate
from sidequest_bot.plugins.items import WOWHEAD_URL
from sidequest_common.schemas import CouncilLogEntry, CouncilLogKind, GuildConfigUpdate

logger = logging.getLogger(__name__)

SYNC_INTERVAL_SECONDS = 120

COUNCIL_KIND_LABELS: dict[str, str] = {
    "chase": "Chase item",
    "legendary": "Legendary or special item",
    "tier": "Crafted tier",
}

QUALITY_COLORS: dict[int, discord.Colour] = {
    0: discord.Colour(0x9D9D9D),  # Poor
    1: discord.Colour(0xFFFFFF),  # Common
    2: discord.Colour(0x1EFF00),  # Uncommon
    3: discord.Colour(0x0070DD),  # Rare
    4: discord.Colour(0xA335EE),  # Epic
    5: discord.Colour(0xFF8000),  # Legendary
}
"""Battle.net's numeric item quality -> WoW's loot color, matching ``plugins.items.QUALITY_COLORS`` by value."""


def quality_color(quality: int) -> discord.Colour:
    """The item's WoW quality color, or the house slate for an unrecognised quality (artifact, heirloom)."""
    return QUALITY_COLORS.get(quality, Colors.SLATE)


def build_decision_embed(entry: CouncilLogEntry) -> discord.Embed:
    """Council award embed: item link + quality color, winner, GP, kind label, reason, raid/boss when known."""
    winner = entry.member_name or entry.character
    embed = make_embed(title=entry.item_name, color=quality_color(entry.quality), timestamp=False)
    embed.url = WOWHEAD_URL.format(item_id=entry.item_id)
    embed.timestamp = entry.at
    embed.add_field(name="Winner", value=winner, inline=True)
    embed.add_field(name="GP", value=str(entry.gp), inline=True)
    if entry.council_kind is not None:
        embed.add_field(name="Kind", value=COUNCIL_KIND_LABELS.get(entry.council_kind, entry.council_kind), inline=True)
    if entry.council_reason:
        embed.add_field(name="Reason", value=truncate(entry.council_reason, 1024), inline=False)
    if entry.instance_name:
        raid = entry.instance_name
        if entry.encounter_name:
            raid = f"{raid} — {entry.encounter_name}"
        embed.add_field(name="Raid", value=raid, inline=False)
    return embed


def build_void_embed(entry: CouncilLogEntry) -> discord.Embed:
    """Short void note: no item link or breakdown, just what got undone."""
    winner = entry.member_name or entry.character
    embed = make_embed(
        description=f"Council award voided: {entry.item_name} to {winner}", color=Colors.RED, timestamp=False
    )
    embed.timestamp = entry.at
    return embed


def build_entry_embed(entry: CouncilLogEntry) -> discord.Embed:
    """``build_decision_embed`` or ``build_void_embed``, whichever matches ``entry.kind``."""
    return build_void_embed(entry) if entry.kind == CouncilLogKind.VOID else build_decision_embed(entry)


class LootLogCog(commands.Cog):
    """Periodic council loot log posting, plus the ``/lootlog`` admin group."""

    lootlog_group = app_commands.Group(
        name="lootlog",
        description="Configure where council loot decisions get posted.",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    def __init__(self, bot: SideQuestBot) -> None:
        self.bot = bot
        self._sync_loop.start()

    async def cog_unload(self) -> None:
        """Stop the post loop when the cog is unloaded."""
        self._sync_loop.cancel()

    @tasks.loop(seconds=SYNC_INTERVAL_SECONDS)
    async def _sync_loop(self) -> None:
        for guild in list(self.bot.guilds):
            try:
                await self._sync_guild(guild)
            except NotFoundError:
                # The API and bot deploy separately; a 404 here is as likely a rolling deploy hitting an old API
                # pod that doesn't have the route yet as a genuinely non-site guild. Never exclude; retry next tick.
                logger.info("Loot council endpoint not available yet for guild %s; retrying next tick", guild.id)
            except APIError:
                # Must not escape: an unhandled exception stops a tasks.loop for good. Retried next tick.
                logger.warning("Loot council sync failed for guild %s", guild.id, exc_info=True)

    @_sync_loop.before_loop
    async def _before_sync_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def _sync_guild(self, guild: discord.Guild) -> None:
        config = await self.bot.configs.get(guild.id)
        if config.loot_log_channel_id is None:
            return
        channel = guild.get_channel(config.loot_log_channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        for entry in await self.bot.api.pending_council_log(guild.id):
            if not await self._post_entry(channel, entry):
                break

    async def _post_entry(self, channel: discord.TextChannel, entry: CouncilLogEntry) -> bool:
        """Post one entry's embed and ack it; ``False`` (without acking) if the channel is no longer postable."""
        try:
            await channel.send(embed=build_entry_embed(entry))
        except discord.Forbidden:
            logger.warning("No permission to post in loot log channel %s; leaving entries pending", channel.id)
            return False
        except discord.NotFound:
            logger.warning("Loot log channel %s no longer exists; leaving entries pending", channel.id)
            return False
        try:
            await self.bot.api.ack_council_log(channel.guild.id, entry.loot_id, entry.kind)
        except APIError:
            logger.warning("Failed to ack loot council entry %s", entry.loot_id, exc_info=True)
        return True

    # --- /lootlog group

    @lootlog_group.command(name="channel", description="Set (or clear) the channel for council loot decisions.")
    @app_commands.describe(channel="Channel to post council loot decisions in; omit to disable.")
    async def lootlog_channel(
        self, interaction: discord.Interaction[SideQuestBot], channel: discord.TextChannel | None = None
    ) -> None:
        """Update ``loot_log_channel_id``."""
        guild = interaction.guild
        if guild is None:
            return
        await interaction.client.configs.update(
            guild.id, GuildConfigUpdate(loot_log_channel_id=channel.id if channel else None)
        )
        message = (
            f"Council loot decisions will post in {channel.mention}." if channel else "Council loot decisions disabled."
        )
        await interaction.response.send_message(message, ephemeral=True)


async def setup(bot: SideQuestBot) -> None:
    """Register the cog."""
    await bot.add_cog(LootLogCog(bot))
