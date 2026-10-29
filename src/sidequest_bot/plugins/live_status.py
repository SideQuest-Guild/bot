"""Live voice-channel/presence status for the homepage "In the Discord" band.

Every 60 s: refresh the guild's approximate online/member counts (``fetch_guild(..., with_counts=True)``) and the
per-channel voice counts visible to the guild's audience role (the roster role if configured and present, else the
linked "Signed the Ledger" role if configured and present, else ``@everyone``), then push a full report.
``on_voice_state_update`` also pushes, debounced ~5 s per guild so a burst of joins/leaves (e.g. everyone piling
into one channel) coalesces into a single push; mute/deafen toggles (channel unchanged) are ignored entirely. See
``local://discord-live-contract.md`` for the wire contract; the API decides what the website shows, this cog only
reports what Discord currently looks like.
"""

import asyncio
import logging
from typing import TYPE_CHECKING

import discord
from discord.ext import commands, tasks

from sidequest_bot.api_client import APIError, NotFoundError
from sidequest_bot.bot import SideQuestBot  # noqa: TC001  # discord.py evaluates app-command annotations at runtime
from sidequest_common.schemas import DiscordLiveReport, GuildRoleIn, VoiceChannelCount

if TYPE_CHECKING:
    from sidequest_common.schemas import GuildConfigSchema

logger = logging.getLogger(__name__)

REPORT_INTERVAL_SECONDS = 60
DEBOUNCE_SECONDS = 5


def build_voice_counts(guild: discord.Guild, audience: discord.Role) -> list[VoiceChannelCount]:
    """Every voice/stage channel ``audience`` can view, in Discord's displayed order, zero counts included."""
    channels = [
        channel
        for channel in (*guild.voice_channels, *guild.stage_channels)
        if channel.permissions_for(audience).view_channel
    ]
    channels.sort(
        key=lambda channel: (
            (channel.category.position, channel.category.id) if channel.category else (-1, 0),
            channel.position,
        )
    )
    return [
        VoiceChannelCount(name=channel.name, count=sum(1 for member in channel.members if not member.bot))
        for channel in channels
    ]


def build_guild_roles(guild: discord.Guild) -> list[GuildRoleIn]:
    """Every Discord role in ``guild`` except ``@everyone`` (its id equals the guild's id)."""
    return [
        GuildRoleIn(id=role.id, name=role.name, color=role.color.value, position=role.position, managed=role.managed)
        for role in guild.roles
        if role.id != guild.id
    ]


class LiveStatusCog(commands.Cog):
    """Periodic push of voice/presence counts, plus an immediate debounced push on voice-state changes."""

    def __init__(self, bot: SideQuestBot) -> None:
        self.bot = bot
        self._not_site_guild_ids: set[int] = set()
        self._last_online: dict[int, int | None] = {}
        self._last_members: dict[int, int | None] = {}
        self._debounce_tasks: dict[int, asyncio.Task[None]] = {}
        self._report_loop.start()

    async def cog_unload(self) -> None:
        """Stop the report loop and any pending debounced pushes."""
        self._report_loop.cancel()
        for task in self._debounce_tasks.values():
            task.cancel()

    # --- Periodic push -----------------------------------------------------------------------------------------

    @tasks.loop(seconds=REPORT_INTERVAL_SECONDS)
    async def _report_loop(self) -> None:
        for guild in list(self.bot.guilds):
            if guild.id in self._not_site_guild_ids:
                continue
            await self._refresh_and_push(guild)

    @_report_loop.before_loop
    async def _before_report_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def _refresh_and_push(self, guild: discord.Guild) -> None:
        """Refresh ``online``/``members`` from Discord, then push the full report."""
        try:
            fetched = await self.bot.fetch_guild(guild.id, with_counts=True)
        except discord.HTTPException:
            logger.debug("Failed to fetch counts for guild %s; keeping last known values", guild.id, exc_info=True)
        else:
            self._last_online[guild.id] = fetched.approximate_presence_count
            self._last_members[guild.id] = fetched.approximate_member_count
        await self._push(guild)

    @staticmethod
    def _resolve_audience_role(guild: discord.Guild, config: GuildConfigSchema) -> discord.Role | None:
        """The roster role if configured and present, else the linked role if configured and present, else None."""
        for role_id in (config.roster_role_id, config.linked_role_id):
            if role_id is not None and (role := guild.get_role(role_id)) is not None:
                return role
        return None

    async def _push(self, guild: discord.Guild) -> None:
        """Report the current voice counts plus the last known online/member counts."""
        audience = guild.default_role
        try:
            config = await self.bot.configs.get(guild.id)
        except APIError:
            logger.warning(
                "Failed to load guild config for %s; using @everyone as the voice-count audience",
                guild.id,
                exc_info=True,
            )
        else:
            audience = self._resolve_audience_role(guild, config) or audience
        report = DiscordLiveReport(
            online=self._last_online.get(guild.id),
            members=self._last_members.get(guild.id),
            voice=build_voice_counts(guild, audience),
        )
        try:
            await self.bot.api.report_discord_live(guild.id, report)
        except NotFoundError:
            logger.info("Guild %s is not the site guild; no longer reporting live status there", guild.id)
            self._not_site_guild_ids.add(guild.id)
            return
        except APIError:
            logger.warning("Failed to report live status for guild %s", guild.id, exc_info=True)
        await self._push_roles(guild)

    async def _push_roles(self, guild: discord.Guild) -> None:
        """Push the guild's full current role set, tolerant of a non-site guild or other API failures."""
        try:
            await self.bot.api.put_guild_roles(guild.id, build_guild_roles(guild))
        except NotFoundError:
            logger.info("Guild %s is not the site guild; no longer pushing roles there", guild.id)
            self._not_site_guild_ids.add(guild.id)
        except APIError:
            logger.warning("Failed to push roles for guild %s", guild.id, exc_info=True)

    # --- Debounced voice-state listener --------------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ) -> None:
        """Schedule a debounced push when a member's voice channel actually changes."""
        if before.channel == after.channel:
            return  # mute/deafen toggle, not a channel change
        guild = member.guild
        if guild.id in self._not_site_guild_ids:
            return
        existing = self._debounce_tasks.get(guild.id)
        if existing is not None:
            existing.cancel()
        self._debounce_tasks[guild.id] = asyncio.ensure_future(self._debounced_push(guild))

    async def _debounced_push(self, guild: discord.Guild) -> None:
        try:
            await asyncio.sleep(DEBOUNCE_SECONDS)
        except asyncio.CancelledError:
            return
        self._debounce_tasks.pop(guild.id, None)
        await self._push(guild)


async def setup(bot: SideQuestBot) -> None:
    """Register the cog."""
    await bot.add_cog(LiveStatusCog(bot))
