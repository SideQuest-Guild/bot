"""Automatic moderation: invite/link blocking, mention-spam and message-flood detection, plus the config commands.

Detection logic is kept in small pure functions (and the injectable-clock :class:`FloodTracker`) so thresholds and
the "0 disables" rule can be unit tested without a live Discord connection. The listener itself stays thin: it reads
config, calls the detectors, and dispatches to :meth:`AutomodCog._enforce`.
"""

import contextlib
import logging
import re
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Literal, cast

import discord
from discord import app_commands
from discord.ext import commands, tasks

from sidequest_bot.api_client import APIError
from sidequest_bot.bot import SideQuestBot  # noqa: TC001  # discord.py evaluates app-command annotations at runtime
from sidequest_bot.lib.embeds import Colors, make_embed
from sidequest_bot.plugins.moderation import build_case_embed, resolve_violation
from sidequest_common.schemas import CaseAction, GuildConfigUpdate, ModCaseCreate

logger = logging.getLogger(__name__)

_INVITE_PATTERN = re.compile(r"(?:discord\.gg|discord(?:app)?\.com/invite)/\S+", re.IGNORECASE)
_LINK_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)

FLOOD_TRACKER_SWEEP_INTERVAL_MINUTES = 10
FLOOD_TRACKER_MAX_IDLE_SECONDS = 3600.0

if TYPE_CHECKING:
    from collections.abc import Callable

    from sidequest_common.schemas import GuildConfigSchema


@dataclass(frozen=True)
class AutomodRule:
    """One triggered rule: what to call it, whether to post a channel notice, whether to also time out."""

    name: str
    notify: bool
    timeout: bool


RULE_INVITE = AutomodRule(name="invite link", notify=True, timeout=False)
RULE_LINK = AutomodRule(name="link", notify=True, timeout=False)
RULE_MENTION_SPAM = AutomodRule(name="mention spam", notify=False, timeout=True)
RULE_FLOOD = AutomodRule(name="message flood", notify=False, timeout=True)


# --- Pure detectors (unit tested directly) ---------------------------------------------------------------------


def contains_invite(content: str) -> bool:
    """Whether ``content`` contains a Discord invite link."""
    return bool(_INVITE_PATTERN.search(content))


def contains_link(content: str) -> bool:
    """Whether ``content`` contains any http(s) link."""
    return bool(_LINK_PATTERN.search(content))


def detect_content_violation(content: str, *, block_invites: bool, block_links: bool) -> AutomodRule | None:
    """Invite check takes priority over the generic link check (an invite is also a link)."""
    if block_invites and contains_invite(content):
        return RULE_INVITE
    if block_links and contains_link(content):
        return RULE_LINK
    return None


def is_mention_spam(user_mention_ids: list[int], role_mention_ids: list[int], *, max_mentions: int) -> bool:
    """More than ``max_mentions`` unique user+role mentions. ``max_mentions <= 0`` disables the check."""
    if max_mentions <= 0:
        return False
    return len(set(user_mention_ids) | set(role_mention_ids)) > max_mentions


class FloodTracker:
    """Sliding-window message-rate tracker keyed by ``(guild_id, user_id)``, with an injectable clock for tests."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._timestamps: dict[tuple[int, int], list[float]] = {}

    def hit(self, guild_id: int, user_id: int, window_seconds: float) -> int:
        """Record a message now; return how many messages from this user fall within the trailing window."""
        key = (guild_id, user_id)
        now = self._clock()
        cutoff = now - window_seconds
        timestamps = [t for t in self._timestamps.get(key, []) if t > cutoff]
        timestamps.append(now)
        self._timestamps[key] = timestamps
        return len(timestamps)

    def sweep(self, max_idle_seconds: float) -> None:
        """Drop tracked ``(guild_id, user_id)`` keys whose most recent hit is older than ``max_idle_seconds``.

        Housekeeping for long-running processes: without this, every user who ever triggers a single hit stays in
        memory forever even after their window has long since emptied.
        """
        cutoff = self._clock() - max_idle_seconds
        stale_keys = [key for key, timestamps in self._timestamps.items() if not timestamps or timestamps[-1] <= cutoff]
        for key in stale_keys:
            del self._timestamps[key]


def is_flooding(hit_count: int, *, max_messages: int) -> bool:
    """More than ``max_messages`` within the tracked window. ``max_messages <= 0`` disables the check."""
    if max_messages <= 0:
        return False
    return hit_count > max_messages


# --- Cog -----------------------------------------------------------------------------------------------------------


class AutomodCog(commands.Cog):
    """``on_message`` filters plus the ``/automod`` config group."""

    automod_group = app_commands.Group(
        name="automod",
        description="Configure the automatic moderation filters.",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )
    exempt_group = app_commands.Group(
        name="exempt", description="Manage automod-exempt roles and channels.", parent=automod_group
    )

    def __init__(self, bot: SideQuestBot) -> None:
        self.bot = bot
        self.flood_tracker = FloodTracker()
        self._sweep_flood_tracker.start()

    async def cog_unload(self) -> None:
        """Stop the housekeeping loop when the cog is unloaded."""
        self._sweep_flood_tracker.cancel()

    @tasks.loop(minutes=FLOOD_TRACKER_SWEEP_INTERVAL_MINUTES)
    async def _sweep_flood_tracker(self) -> None:
        """Periodically drop idle flood-tracker entries so memory doesn't grow unbounded."""
        self.flood_tracker.sweep(FLOOD_TRACKER_MAX_IDLE_SECONDS)

    # --- Listener -----------------------------------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Run the filters for one message, never letting an API/Discord error crash the listener."""
        if message.guild is None or message.author.bot:
            return
        try:
            await self._moderate(message)
        except APIError, discord.HTTPException:
            logger.warning("Automod failed for message %s in guild %s", message.id, message.guild.id, exc_info=True)

    async def _moderate(self, message: discord.Message) -> None:
        guild = message.guild
        assert guild is not None  # noqa: S101 — checked by the caller
        member = message.author
        if not isinstance(member, discord.Member):
            return
        config = await self.bot.configs.get(guild.id)
        if not config.automod_enabled:
            return
        if member.guild_permissions.manage_messages:
            return
        if any(role.id in config.automod_exempt_role_ids for role in member.roles):
            return
        if message.channel.id in config.automod_exempt_channel_ids:
            return

        rule = detect_content_violation(
            message.content, block_invites=config.automod_block_invites, block_links=config.automod_block_links
        )
        if rule is None and is_mention_spam(
            [u.id for u in message.mentions],
            [r.id for r in message.role_mentions],
            max_mentions=config.automod_max_mentions,
        ):
            rule = RULE_MENTION_SPAM
        if rule is None and config.automod_spam_messages > 0:
            hit_count = self.flood_tracker.hit(guild.id, member.id, config.automod_spam_seconds)
            if is_flooding(hit_count, max_messages=config.automod_spam_messages):
                rule = RULE_FLOOD
        if rule is None:
            return
        await self._enforce(message, member, config, rule)

    async def _enforce(
        self, message: discord.Message, member: discord.Member, config: GuildConfigSchema, rule: AutomodRule
    ) -> None:
        guild = message.guild
        assert guild is not None  # noqa: S101
        with contextlib.suppress(discord.NotFound):
            await message.delete()

        if rule.notify:
            await self._notify_channel(message.channel, member, rule)

        duration_seconds: int | None = None
        if rule.timeout and config.automod_timeout_seconds > 0:
            bot_member = guild.me
            can_timeout = (
                resolve_violation(actor=bot_member, target_id=member.id, target_member=member, bot_member=bot_member)
                is None
            )
            if can_timeout:
                try:
                    await member.timeout(
                        timedelta(seconds=config.automod_timeout_seconds), reason=f"Automod: {rule.name}"
                    )
                except discord.HTTPException:
                    logger.warning("Could not time out %s for automod in guild %s", member.id, guild.id, exc_info=True)
                else:
                    duration_seconds = config.automod_timeout_seconds

        bot_user = self.bot.user
        moderator_id = bot_user.id if bot_user is not None else member.id
        case = await self.bot.api.create_case(
            guild.id,
            ModCaseCreate(
                action=CaseAction.AUTOMOD,
                user_id=member.id,
                moderator_id=moderator_id,
                reason=rule.name,
                duration_seconds=duration_seconds,
            ),
        )
        moderator_line = bot_user.mention if bot_user is not None else "Automod"
        embed = build_case_embed(case, target_line=f"<@{member.id}>", moderator_line=moderator_line)
        await self.bot.send_log(guild, embed)

    @staticmethod
    async def _notify_channel(
        channel: discord.abc.MessageableChannel, member: discord.Member, rule: AutomodRule
    ) -> None:
        try:
            await channel.send(
                f"{member.mention}, that message was removed ({rule.name}).",
                delete_after=10,
                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
            )
        except discord.HTTPException:
            logger.warning("Could not post automod notice in channel %s", channel.id, exc_info=True)

    # --- /automod group -------------------------------------------------------------------------------------------

    @automod_group.command(name="status", description="Show the current automod configuration.")
    async def status(self, interaction: discord.Interaction[SideQuestBot]) -> None:
        """Dump every automod setting."""
        guild_id = cast("int", interaction.guild_id)
        config = await interaction.client.configs.get(guild_id)
        lines = [
            f"Enabled: {config.automod_enabled}",
            f"Block invites: {config.automod_block_invites}",
            f"Block links: {config.automod_block_links}",
            f"Max mentions: {config.automod_max_mentions} (0 = off)",
            f"Spam threshold: {config.automod_spam_messages} messages / {config.automod_spam_seconds}s (0 = off)",
            f"Automod timeout: {config.automod_timeout_seconds}s (0 = off)",
            f"Exempt roles: {', '.join(f'<@&{r}>' for r in config.automod_exempt_role_ids) or 'none'}",
            f"Exempt channels: {', '.join(f'<#{c}>' for c in config.automod_exempt_channel_ids) or 'none'}",
        ]
        embed = make_embed(title="Automod configuration", description="\n".join(lines), color=Colors.BLUE)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @automod_group.command(name="toggle", description="Turn a filter on or off.")
    @app_commands.describe(setting="Which setting to change.", value="On or off.")
    async def toggle(
        self,
        interaction: discord.Interaction[SideQuestBot],
        setting: Literal["enabled", "invites", "links"],
        *,
        value: bool,
    ) -> None:
        """Flip ``automod_enabled``/``automod_block_invites``/``automod_block_links``."""
        guild_id = cast("int", interaction.guild_id)
        if setting == "enabled":
            changes = GuildConfigUpdate(automod_enabled=value)
        elif setting == "invites":
            changes = GuildConfigUpdate(automod_block_invites=value)
        else:
            changes = GuildConfigUpdate(automod_block_links=value)
        await interaction.client.configs.update(guild_id, changes)
        await interaction.response.send_message(f"{setting} is now {'on' if value else 'off'}.", ephemeral=True)

    @automod_group.command(name="mentions", description="Set the mention-spam threshold (0 disables it).")
    @app_commands.describe(n="Unique user+role mentions allowed per message before action is taken.")
    async def mentions(
        self, interaction: discord.Interaction[SideQuestBot], n: app_commands.Range[int, 0, 100]
    ) -> None:
        """Update ``automod_max_mentions``."""
        await interaction.client.configs.update(
            cast("int", interaction.guild_id), GuildConfigUpdate(automod_max_mentions=n)
        )
        await interaction.response.send_message(f"Mention-spam threshold set to {n} (0 = off).", ephemeral=True)

    @automod_group.command(name="spam", description="Set the message-flood threshold (0 messages disables it).")
    @app_commands.describe(messages="Messages allowed per window before action is taken.", seconds="Window length.")
    async def spam(
        self,
        interaction: discord.Interaction[SideQuestBot],
        messages: app_commands.Range[int, 0, 100],
        seconds: app_commands.Range[int, 1, 600],
    ) -> None:
        """Update ``automod_spam_messages``/``automod_spam_seconds``."""
        await interaction.client.configs.update(
            cast("int", interaction.guild_id),
            GuildConfigUpdate(automod_spam_messages=messages, automod_spam_seconds=seconds),
        )
        await interaction.response.send_message(
            f"Flood threshold set to {messages} messages / {seconds}s (0 messages = off).", ephemeral=True
        )

    @automod_group.command(name="timeout", description="Set the automod timeout duration (0 disables timing out).")
    @app_commands.describe(seconds="How long to time out offenders for mention spam/flooding.")
    async def timeout_setting(
        self, interaction: discord.Interaction[SideQuestBot], seconds: app_commands.Range[int, 0, 2_419_200]
    ) -> None:
        """Update ``automod_timeout_seconds``."""
        await interaction.client.configs.update(
            cast("int", interaction.guild_id), GuildConfigUpdate(automod_timeout_seconds=seconds)
        )
        await interaction.response.send_message(f"Automod timeout set to {seconds}s (0 = off).", ephemeral=True)

    @exempt_group.command(name="role", description="Add or remove a role from the automod exemption list.")
    @app_commands.describe(action="Add or remove.", role="The role.")
    async def exempt_role(
        self, interaction: discord.Interaction[SideQuestBot], action: Literal["add", "remove"], role: discord.Role
    ) -> None:
        """Update ``automod_exempt_role_ids``."""
        guild_id = cast("int", interaction.guild_id)
        config = await interaction.client.configs.get(guild_id)
        role_ids = set(config.automod_exempt_role_ids)
        role_ids.add(role.id) if action == "add" else role_ids.discard(role.id)
        await interaction.client.configs.update(guild_id, GuildConfigUpdate(automod_exempt_role_ids=sorted(role_ids)))
        verb = "exempted" if action == "add" else "no longer exempted"
        await interaction.response.send_message(f"{role.mention} is {verb}.", ephemeral=True)

    @exempt_group.command(name="channel", description="Add or remove a channel from the automod exemption list.")
    @app_commands.describe(action="Add or remove.", channel="The channel.")
    async def exempt_channel(
        self,
        interaction: discord.Interaction[SideQuestBot],
        action: Literal["add", "remove"],
        channel: discord.abc.GuildChannel,
    ) -> None:
        """Update ``automod_exempt_channel_ids``."""
        guild_id = cast("int", interaction.guild_id)
        config = await interaction.client.configs.get(guild_id)
        channel_ids = set(config.automod_exempt_channel_ids)
        channel_ids.add(channel.id) if action == "add" else channel_ids.discard(channel.id)
        await interaction.client.configs.update(
            guild_id, GuildConfigUpdate(automod_exempt_channel_ids=sorted(channel_ids))
        )
        verb = "exempted" if action == "add" else "no longer exempted"
        await interaction.response.send_message(f"{channel.mention} is {verb}.", ephemeral=True)


async def setup(bot: SideQuestBot) -> None:
    """Register the cog."""
    await bot.add_cog(AutomodCog(bot))
