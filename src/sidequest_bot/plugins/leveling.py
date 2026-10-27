"""Leveling: XP on chat, level-up announcements and role rewards, `/rank`, `/leaderboard`, `/levels` admin."""

import logging
import math
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from sidequest_bot.api_client import APIError
from sidequest_bot.bot import SideQuestBot  # noqa: TC001  # discord.py evaluates app-command annotations at runtime
from sidequest_bot.lib.embeds import Colors, make_embed
from sidequest_bot.lib.roles import configure_block_reason, grant_block_reason
from sidequest_common.schemas import GuildConfigUpdate
from sidequest_common.schemas.levels import MAX_XP  # noqa: TC001  # used at runtime in app_commands.Range
from sidequest_common.templates import TEMPLATE_PLACEHOLDERS, render_template

if TYPE_CHECKING:
    from sidequest_common.schemas import GuildConfigSchema, XPAwardResult

logger = logging.getLogger(__name__)

LEADERBOARD_PAGE_SIZE = 10
PROGRESS_BAR_LENGTH = 12


def render_progress_bar(current: int, total: int, *, length: int = PROGRESS_BAR_LENGTH) -> str:
    """A ``length``-wide filled/empty bar for ``current`` out of ``total``."""
    ratio = 1.0 if total <= 0 else max(0.0, min(1.0, current / total))
    filled = max(0, min(length, round(ratio * length)))
    return "█" * filled + "░" * (length - filled)


def _message_channel_ids(message: discord.Message) -> set[int]:
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
    if _message_channel_ids(message) & set(config.no_xp_channel_ids):
        return False
    author_role_ids = {role.id for role in getattr(message.author, "roles", [])}
    return not author_role_ids & set(config.no_xp_role_ids)


class LevelingCog(commands.Cog):
    """Awards XP for chat activity and announces/applies level-ups."""

    def __init__(self, bot: SideQuestBot) -> None:
        self.bot = bot

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
        if not result.leveled_up:
            return
        try:
            await self._handle_level_up(message, config, result)
        except discord.HTTPException:
            logger.warning("Failed to announce or apply level-up for message %s", message.id, exc_info=True)

    async def _handle_level_up(
        self, message: discord.Message, config: GuildConfigSchema, result: XPAwardResult
    ) -> None:
        guild = message.guild
        if guild is None:
            return
        text = render_template(
            config.level_up_message,
            mention=message.author.mention,
            user=message.author.display_name,
            guild=guild.name,
            level=result.level,
        )
        target: discord.abc.Messageable = message.channel
        if config.level_up_channel_id is not None:
            channel = guild.get_channel_or_thread(config.level_up_channel_id)
            if isinstance(channel, discord.abc.Messageable):
                target = channel
        try:
            await target.send(text, allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
        except discord.HTTPException:
            logger.warning("Failed to post level-up announcement for message %s", message.id, exc_info=True)
        if result.earned_role_ids or result.remove_role_ids:
            await self._apply_role_deltas(guild, message.author, result.earned_role_ids, result.remove_role_ids)

    async def _apply_role_deltas(
        self,
        guild: discord.Guild,
        member: discord.User | discord.Member,
        earned_role_ids: list[int],
        remove_role_ids: list[int],
    ) -> None:
        if not isinstance(member, discord.Member):
            return
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
        except discord.Forbidden:
            logger.warning("Missing permission to update level roles in guild %s", guild.id, exc_info=True)


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
