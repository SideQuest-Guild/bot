"""Leveling: XP for chat, voice, events and #lfg groups, level-ups and level roles, `/rank`, `/leaderboard`, `/levels`.

Chat XP is per message (``on_message``). Every minute, members in voice with someone else, not deafened, outside the
AFK channel and no-XP channels or roles get voice XP. Every five minutes the API pays any newly earned event and group
XP. The API sets the amounts (``VOICE_XP_PER_MINUTE``, ``EVENT_XP``, ``GROUP_XP``) and keeps each event and group to
one payout; the bot only decides who's in voice and announces the level-ups.
"""

import logging
import math
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands, tasks

from sidequest_bot.api_client import APIError, NotFoundError
from sidequest_bot.bot import SideQuestBot  # noqa: TC001  # discord.py evaluates app-command annotations at runtime
from sidequest_bot.lib.embeds import Colors, make_embed
from sidequest_bot.lib.roles import configure_block_reason, grant_block_reason
from sidequest_common.schemas import GuildConfigUpdate, VoiceXPRequest, XPAwardResult
from sidequest_common.schemas.levels import MAX_XP  # noqa: TC001  # used at runtime in app_commands.Range
from sidequest_common.templates import TEMPLATE_PLACEHOLDERS, render_template

if TYPE_CHECKING:
    from collections.abc import Iterable

    from sidequest_common.schemas import GuildConfigSchema, XPGrantResult

logger = logging.getLogger(__name__)

LEADERBOARD_PAGE_SIZE = 10
PROGRESS_BAR_LENGTH = 12
VOICE_TICK_SECONDS = 60
ACTIVITY_SYNC_SECONDS = 300
MIN_VOICE_COMPANY = 2
"""Members who must be listening in a channel for any of them to earn voice XP, so sitting alone earns nothing."""

type VoiceChannel = discord.VoiceChannel | discord.StageChannel


def render_progress_bar(current: int, total: int, *, length: int = PROGRESS_BAR_LENGTH) -> str:
    """A ``length``-wide filled/empty bar for ``current`` out of ``total``."""
    ratio = 1.0 if total <= 0 else max(0.0, min(1.0, current / total))
    filled = max(0, min(length, round(ratio * length)))
    return "█" * filled + "░" * (length - filled)


def message_channel_ids(message: discord.Message) -> set[int]:
    """The channel's own ID, plus its thread parent's ID if it's a thread."""
    channel = message.channel
    ids = {channel.id}
    parent_id = getattr(channel, "parent_id", None)
    if parent_id is not None:
        ids.add(parent_id)
    return ids


def is_xp_eligible(message: discord.Message, config: GuildConfigSchema) -> bool:
    """Whether ``message`` should earn XP under ``config``.

    Excludes bots, webhooks, DMs, system messages, a disabled leveling system, no-XP channels (or their thread's
    parent channel), and members holding a no-XP role.
    """
    if message.author.bot or message.webhook_id is not None:
        return False
    if message.guild is None:
        return False
    if message.is_system():
        return False
    if not config.leveling_enabled:
        return False
    if message_channel_ids(message) & set(config.no_xp_channel_ids):
        return False
    author_role_ids = {role.id for role in getattr(message.author, "roles", [])}
    return not author_role_ids & set(config.no_xp_role_ids)


def _listening(member: discord.Member) -> bool:
    voice = member.voice
    return voice is not None and not (voice.self_deaf or voice.deaf)


def voice_xp_earners(guild: discord.Guild, config: GuildConfigSchema) -> dict[int, VoiceChannel]:
    """Members earning voice XP this minute, each with the channel they're in.

    A channel counts when at least ``MIN_VOICE_COMPANY`` non-bot members in it aren't deafened, and it isn't the AFK
    channel or a no-XP channel (or in a no-XP category). Members holding a no-XP role are left out.
    """
    if not config.leveling_enabled:
        return {}
    afk_id = guild.afk_channel.id if guild.afk_channel is not None else None
    no_xp_channels = set(config.no_xp_channel_ids)
    no_xp_roles = set(config.no_xp_role_ids)
    earners: dict[int, VoiceChannel] = {}
    for channel in (*guild.voice_channels, *guild.stage_channels):
        if channel.id == afk_id or {channel.id, channel.category_id} & no_xp_channels:
            continue
        listening = [member for member in channel.members if not member.bot and _listening(member)]
        if len(listening) < MIN_VOICE_COMPANY:
            continue
        for member in listening:
            if not {role.id for role in member.roles} & no_xp_roles:
                earners[member.id] = channel
    return earners


def voice_present_members(guild: discord.Guild) -> dict[int, VoiceChannel]:
    """Members present under the server-stats rule this minute, each with the channel they're in.

    A channel counts when at least ``MIN_VOICE_COMPANY`` non-bot members are in it, deafened or not, and it isn't the
    AFK channel. Unlike :func:`voice_xp_earners`, no-XP channels/roles and ``leveling_enabled`` don't apply: this is
    presence for server stats, not voice XP eligibility.
    """
    afk_id = guild.afk_channel.id if guild.afk_channel is not None else None
    present: dict[int, VoiceChannel] = {}
    for channel in (*guild.voice_channels, *guild.stage_channels):
        if channel.id == afk_id:
            continue
        members = [member for member in channel.members if not member.bot]
        if len(members) < MIN_VOICE_COMPANY:
            continue
        for member in members:
            present[member.id] = channel
    return present


def merge_grants(grants: Iterable[XPGrantResult]) -> dict[int, XPAwardResult]:
    """One result per member, so two grants in one sync announce one level-up with every role change."""
    merged: dict[int, XPAwardResult] = {}
    for grant in grants:
        previous = merged.get(grant.user_id)
        if previous is None:
            merged[grant.user_id] = XPAwardResult.model_validate(grant.model_dump(exclude={"user_id", "source"}))
            continue
        removed = {*previous.remove_role_ids, *grant.remove_role_ids}
        earned = [role_id for role_id in (*previous.earned_role_ids, *grant.earned_role_ids) if role_id not in removed]
        merged[grant.user_id] = XPAwardResult(
            awarded=True,
            amount=previous.amount + grant.amount,
            xp=grant.xp,
            level=grant.level,
            previous_level=previous.previous_level,
            leveled_up=grant.level > previous.previous_level,
            earned_role_ids=list(dict.fromkeys(earned)),
            remove_role_ids=sorted(removed),
        )
    return merged


class LevelingCog(commands.Cog):
    """Awards XP for chat and voice, pays event and group XP, and announces/applies level-ups."""

    def __init__(self, bot: SideQuestBot) -> None:
        self.bot = bot
        self._voice_tick.start()
        self._activity_sync.start()

    async def cog_unload(self) -> None:
        """Stop the voice and activity loops."""
        self._voice_tick.cancel()
        self._activity_sync.cancel()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Award XP for an eligible message, then announce and apply any level-up."""
        if message.guild is None:
            return
        try:
            config = await self.bot.configs.get(message.guild.id)
            if not is_xp_eligible(message, config):
                return
            result = await self.bot.api.award_xp(message.guild.id, message.author.id)
        except APIError:
            logger.warning("Failed to award XP for message %s", message.id, exc_info=True)
            return
        if result.leveled_up and isinstance(message.author, discord.Member):
            await self.handle_level_up(message.guild, message.author, config, result, fallback=message.channel)

    @tasks.loop(seconds=VOICE_TICK_SECONDS)
    async def _voice_tick(self) -> None:
        for guild in list(self.bot.guilds):
            try:
                await self.grant_voice_xp(guild)
            except NotFoundError:
                logger.info("Voice XP route not available yet for guild %s; retrying next minute", guild.id)
            except APIError:
                # Must not escape: an unhandled exception stops a tasks.loop for good.
                logger.warning("Voice XP failed for guild %s", guild.id, exc_info=True)

    @_voice_tick.before_loop
    async def _before_voice_tick(self) -> None:
        await self.bot.wait_until_ready()

    async def grant_voice_xp(self, guild: discord.Guild) -> None:
        """Give this minute's voice XP, report presence for stats, and announce any level-ups."""
        config = await self.bot.configs.get(guild.id)
        earners = voice_xp_earners(guild, config)
        present = voice_present_members(guild)
        if not present:
            return
        grants = await self.bot.api.grant_voice_xp(
            guild.id, VoiceXPRequest(user_ids=sorted(earners), present_user_ids=sorted(present))
        )
        for user_id, result in merge_grants(grants).items():
            member = guild.get_member(user_id)
            if result.leveled_up and member is not None:
                await self.handle_level_up(guild, member, config, result, fallback=earners.get(user_id))

    @tasks.loop(seconds=ACTIVITY_SYNC_SECONDS)
    async def _activity_sync(self) -> None:
        for guild in list(self.bot.guilds):
            try:
                await self.sync_activity_xp(guild)
            except NotFoundError:
                # Not the site guild, or an API pod without the route yet mid-deploy. Retried next time either way.
                logger.info("Activity XP not available for guild %s", guild.id)
            except APIError:
                logger.warning("Activity XP sync failed for guild %s", guild.id, exc_info=True)

    @_activity_sync.before_loop
    async def _before_activity_sync(self) -> None:
        await self.bot.wait_until_ready()

    async def sync_activity_xp(self, guild: discord.Guild) -> None:
        """Collect event and group XP; announce level-ups in the level-up channel or the server's system channel."""
        report = await self.bot.api.sync_activity_xp(guild.id)
        if not report.grants:
            return
        config = await self.bot.configs.get(guild.id)
        for user_id, result in merge_grants(report.grants).items():
            member = guild.get_member(user_id)
            if result.leveled_up and member is not None:
                await self.handle_level_up(guild, member, config, result, fallback=guild.system_channel)

    async def handle_level_up(
        self,
        guild: discord.Guild,
        member: discord.Member,
        config: GuildConfigSchema,
        result: XPAwardResult,
        *,
        fallback: discord.abc.Messageable | None,
    ) -> None:
        """Post the level-up message (to the level-up channel if set, else ``fallback``) and apply level roles."""
        text = render_template(
            config.level_up_message,
            mention=member.mention,
            user=member.display_name,
            guild=guild.name,
            level=result.level,
        )
        target = fallback
        if config.level_up_channel_id is not None:
            channel = guild.get_channel_or_thread(config.level_up_channel_id)
            if isinstance(channel, discord.abc.Messageable):
                target = channel
        if target is not None:
            try:
                await target.send(
                    text, allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False)
                )
            except discord.HTTPException:
                logger.warning("Failed to post level-up announcement for %s in guild %s", member.id, guild.id)
        if result.earned_role_ids or result.remove_role_ids:
            await self._apply_role_deltas(guild, member, result.earned_role_ids, result.remove_role_ids)

    async def _apply_role_deltas(
        self,
        guild: discord.Guild,
        member: discord.Member,
        earned_role_ids: list[int],
        remove_role_ids: list[int],
    ) -> None:
        to_add = [
            role
            for role_id in earned_role_ids
            if (role := guild.get_role(role_id)) is not None and grant_block_reason(role) is None
        ]
        to_remove = [
            role
            for role_id in remove_role_ids
            if (role := guild.get_role(role_id)) is not None and grant_block_reason(role) is None
        ]
        try:
            if to_add:
                await member.add_roles(*to_add, reason="Level-up reward")
            if to_remove:
                await member.remove_roles(*to_remove, reason="Level-up role cleanup")
        except discord.HTTPException:
            logger.warning("Couldn't update level roles for %s in guild %s", member.id, guild.id, exc_info=True)


# --- /rank, /leaderboard


@app_commands.command(name="rank", description="Show a member's level and XP.")
@app_commands.describe(member="Member to check (defaults to you)")
@app_commands.guild_only()
async def rank_command(interaction: discord.Interaction[SideQuestBot], member: discord.Member | None = None) -> None:
    """Public embed with level, XP progress, rank, and message count."""
    target = member or interaction.user
    if interaction.guild_id is None:
        return
    data = await interaction.client.api.get_rank(interaction.guild_id, target.id)
    bar = render_progress_bar(data.xp_into_level, data.xp_for_next_level)
    embed = make_embed(title=f"{target.display_name}'s Rank", color=Colors.GOLD)
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.add_field(name="Level", value=str(data.level))
    embed.add_field(name="Rank", value=f"#{data.rank}" if data.rank else "Unranked")
    embed.add_field(name="Messages", value=str(data.message_count))
    embed.add_field(name="Progress", value=f"{bar}\n{data.xp_into_level}/{data.xp_for_next_level} XP", inline=False)
    await interaction.response.send_message(embed=embed)


@app_commands.command(name="leaderboard", description="Show the server XP leaderboard.")
@app_commands.describe(page="Page number (1-based)")
@app_commands.guild_only()
async def leaderboard_command(
    interaction: discord.Interaction[SideQuestBot], page: app_commands.Range[int, 1, None] = 1
) -> None:
    """10 entries per page, highest XP first."""
    if interaction.guild_id is None:
        return
    offset = (page - 1) * LEADERBOARD_PAGE_SIZE
    board = await interaction.client.api.get_leaderboard(
        interaction.guild_id, limit=LEADERBOARD_PAGE_SIZE, offset=offset
    )
    if not board.entries:
        await interaction.response.send_message("No one has earned XP yet.", ephemeral=True)
        return
    guild = interaction.guild
    lines = []
    for entry in board.entries:
        member = guild.get_member(entry.user_id) if guild is not None else None
        name = member.display_name if member is not None else f"<@{entry.user_id}>"
        lines.append(f"**#{entry.rank}** {name} — Level {entry.level} ({entry.xp} XP)")
    total_pages = max(1, math.ceil(board.total / LEADERBOARD_PAGE_SIZE))
    embed = make_embed(title="Leaderboard", description="\n".join(lines), color=Colors.GOLD)
    embed.set_footer(text=f"Page {page}/{total_pages} • {board.total} ranked member(s)")
    await interaction.response.send_message(embed=embed)


# --- /levels admin group

levels_group = app_commands.Group(
    name="levels",
    description="Configure the leveling system.",
    guild_only=True,
    default_permissions=discord.Permissions(manage_guild=True),
)
reward_group = app_commands.Group(name="reward", description="Manage level-up role rewards.", parent=levels_group)
config_group = app_commands.Group(name="config", description="Tune leveling behavior.", parent=levels_group)


@levels_group.command(name="set-xp", description="Override a member's XP total.")
async def levels_set_xp(
    interaction: discord.Interaction[SideQuestBot], member: discord.Member, xp: app_commands.Range[int, 0, MAX_XP]
) -> None:
    """Admin override: set a member's XP total directly."""
    if interaction.guild_id is None:
        return
    result = await interaction.client.api.set_xp(interaction.guild_id, member.id, xp)
    await interaction.response.send_message(
        f"{member.mention} is now level {result.level} with {result.xp} XP.", ephemeral=True
    )


@reward_group.command(name="set", description="Grant a role when members reach a level.")
async def reward_set(
    interaction: discord.Interaction[SideQuestBot], level: app_commands.Range[int, 1, 1000], role: discord.Role
) -> None:
    """Grant ``role`` at ``level``, replacing any existing reward for that level."""
    if interaction.guild_id is None or not isinstance(interaction.user, discord.Member):
        return
    if (reason := configure_block_reason(interaction.user, role)) is not None:
        await interaction.response.send_message(reason, ephemeral=True)
        return
    if (reason := grant_block_reason(role)) is not None:
        await interaction.response.send_message(reason, ephemeral=True)
        return
    await interaction.client.api.set_level_role(interaction.guild_id, level, role.id)
    await interaction.response.send_message(f"Level {level} now grants {role.mention}.", ephemeral=True)


@reward_group.command(name="remove", description="Remove a level-up role reward.")
async def reward_remove(
    interaction: discord.Interaction[SideQuestBot], level: app_commands.Range[int, 1, 1000]
) -> None:
    """Remove the reward at ``level``."""
    if interaction.guild_id is None:
        return
    await interaction.client.api.delete_level_role(interaction.guild_id, level)
    await interaction.response.send_message(f"Removed the reward at level {level}.", ephemeral=True)


@reward_group.command(name="list", description="List level-up role rewards.")
async def reward_list(interaction: discord.Interaction[SideQuestBot]) -> None:
    """List every level-up role reward, lowest level first."""
    if interaction.guild_id is None:
        return
    rewards = await interaction.client.api.list_level_roles(interaction.guild_id)
    if not rewards:
        await interaction.response.send_message("No level-up rewards configured.", ephemeral=True)
        return
    lines = [f"Level {reward.level} → <@&{reward.role_id}>" for reward in rewards]
    embed = make_embed(title="Level-Up Rewards", description="\n".join(lines), color=Colors.GOLD)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@config_group.command(name="enabled", description="Turn the leveling system on or off.")
async def config_enabled(interaction: discord.Interaction[SideQuestBot], *, enabled: bool) -> None:
    """Enable or disable XP gain for the guild."""
    if interaction.guild_id is None:
        return
    await interaction.client.configs.update(interaction.guild_id, GuildConfigUpdate(leveling_enabled=enabled))
    await interaction.response.send_message(f"Leveling {'enabled' if enabled else 'disabled'}.", ephemeral=True)


@config_group.command(name="level-up-channel", description="Set where level-up messages post (omit for here).")
async def config_level_up_channel(
    interaction: discord.Interaction[SideQuestBot], channel: discord.TextChannel | None = None
) -> None:
    """Set the level-up announcement channel; defaults to the invoking channel."""
    if interaction.guild_id is None:
        return
    target = channel or interaction.channel
    if not isinstance(target, discord.TextChannel | discord.Thread):
        await interaction.response.send_message("Pick a text channel or thread.", ephemeral=True)
        return
    await interaction.client.configs.update(interaction.guild_id, GuildConfigUpdate(level_up_channel_id=target.id))
    await interaction.response.send_message(f"Level-up messages will post in {target.mention}.", ephemeral=True)


@config_group.command(name="level-up-message", description="Set the level-up announcement template.")
@app_commands.describe(message="Template text; see the listed placeholders")
async def config_level_up_message(interaction: discord.Interaction[SideQuestBot], message: str) -> None:
    """Set the level-up message template and show the available placeholders."""
    if interaction.guild_id is None:
        return
    await interaction.client.configs.update(interaction.guild_id, GuildConfigUpdate(level_up_message=message))
    placeholders = "\n".join(f"`{{{name}}}` — {description}" for name, description in TEMPLATE_PLACEHOLDERS.items())
    await interaction.response.send_message(
        f"Level-up message updated.\n\n**Available placeholders:**\n{placeholders}", ephemeral=True
    )


@config_group.command(name="xp-range", description="Set the XP awarded per message.")
async def config_xp_range(
    interaction: discord.Interaction[SideQuestBot],
    minimum: app_commands.Range[int, 0, 1000],
    maximum: app_commands.Range[int, 0, 1000],
) -> None:
    """Set the per-message XP range."""
    if interaction.guild_id is None:
        return
    await interaction.client.configs.update(interaction.guild_id, GuildConfigUpdate(xp_min=minimum, xp_max=maximum))
    await interaction.response.send_message(f"XP per message set to {minimum}-{maximum}.", ephemeral=True)


@config_group.command(name="cooldown", description="Set seconds between XP awards per member.")
async def config_cooldown(
    interaction: discord.Interaction[SideQuestBot], seconds: app_commands.Range[int, 0, 86400]
) -> None:
    """Set the per-member XP cooldown in seconds."""
    if interaction.guild_id is None:
        return
    await interaction.client.configs.update(interaction.guild_id, GuildConfigUpdate(xp_cooldown_seconds=seconds))
    await interaction.response.send_message(f"XP cooldown set to {seconds}s.", ephemeral=True)


@config_group.command(name="stacking", description="Whether members keep lower level-role rewards.")
async def config_stacking(interaction: discord.Interaction[SideQuestBot], *, enabled: bool) -> None:
    """Toggle whether level-role rewards stack."""
    if interaction.guild_id is None:
        return
    await interaction.client.configs.update(interaction.guild_id, GuildConfigUpdate(level_roles_stack=enabled))
    state = "enabled" if enabled else "disabled"
    await interaction.response.send_message(f"Level-role stacking {state}.", ephemeral=True)


@config_group.command(name="no-xp-channel-add", description="Stop a channel from earning XP.")
async def config_no_xp_channel_add(
    interaction: discord.Interaction[SideQuestBot], channel: app_commands.AppCommandChannel
) -> None:
    """Add ``channel`` to the no-XP channel list."""
    if interaction.guild_id is None:
        return
    config = await interaction.client.configs.get(interaction.guild_id)
    ids = sorted({*config.no_xp_channel_ids, channel.id})
    await interaction.client.configs.update(interaction.guild_id, GuildConfigUpdate(no_xp_channel_ids=ids))
    await interaction.response.send_message(f"{channel.mention} no longer earns XP.", ephemeral=True)


@config_group.command(name="no-xp-channel-remove", description="Let a channel earn XP again.")
async def config_no_xp_channel_remove(
    interaction: discord.Interaction[SideQuestBot], channel: app_commands.AppCommandChannel
) -> None:
    """Remove ``channel`` from the no-XP channel list."""
    if interaction.guild_id is None:
        return
    config = await interaction.client.configs.get(interaction.guild_id)
    ids = sorted({cid for cid in config.no_xp_channel_ids if cid != channel.id})
    await interaction.client.configs.update(interaction.guild_id, GuildConfigUpdate(no_xp_channel_ids=ids))
    await interaction.response.send_message(f"{channel.mention} earns XP again.", ephemeral=True)


@config_group.command(name="no-xp-role-add", description="Members with this role stop earning XP.")
async def config_no_xp_role_add(interaction: discord.Interaction[SideQuestBot], role: discord.Role) -> None:
    """Add ``role`` to the no-XP role list."""
    if interaction.guild_id is None:
        return
    config = await interaction.client.configs.get(interaction.guild_id)
    ids = sorted({*config.no_xp_role_ids, role.id})
    await interaction.client.configs.update(interaction.guild_id, GuildConfigUpdate(no_xp_role_ids=ids))
    await interaction.response.send_message(f"Members with {role.mention} no longer earn XP.", ephemeral=True)


@config_group.command(name="no-xp-role-remove", description="Members with this role earn XP again.")
async def config_no_xp_role_remove(interaction: discord.Interaction[SideQuestBot], role: discord.Role) -> None:
    """Remove ``role`` from the no-XP role list."""
    if interaction.guild_id is None:
        return
    config = await interaction.client.configs.get(interaction.guild_id)
    ids = sorted({rid for rid in config.no_xp_role_ids if rid != role.id})
    await interaction.client.configs.update(interaction.guild_id, GuildConfigUpdate(no_xp_role_ids=ids))
    await interaction.response.send_message(f"Members with {role.mention} earn XP again.", ephemeral=True)


async def setup(bot: SideQuestBot) -> None:
    """Register the cog and its top-level app commands."""
    await bot.add_cog(LevelingCog(bot))
    bot.tree.add_command(rank_command)
    bot.tree.add_command(leaderboard_command)
    bot.tree.add_command(levels_group)
