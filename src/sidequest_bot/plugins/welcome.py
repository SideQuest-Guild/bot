"""Member-join welcome message and optional auto-role, plus the ``/welcome`` admin group.

Membership-screening ("pending") members are deferred: both the welcome message and the auto-role wait until
``on_member_update`` reports the member has passed screening, instead of firing while they still can't see the
server.
"""

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from sidequest_bot.bot import SideQuestBot  # noqa: TC001  # discord.py evaluates app-command annotations at runtime
from sidequest_bot.lib.embeds import make_embed
from sidequest_bot.lib.roles import configure_block_reason, grant_block_reason
from sidequest_common.schemas import GuildConfigUpdate
from sidequest_common.templates import TEMPLATE_PLACEHOLDERS, render_template

if TYPE_CHECKING:
    from sidequest_common.schemas import GuildConfigSchema

logger = logging.getLogger(__name__)


# --- Pure helpers (unit tested directly) ---------------------------------------------------------------------


def completed_screening(*, before_pending: bool, after_pending: bool) -> bool:
    """True when a member update reports they just passed membership screening (pending -> not pending)."""
    return before_pending and not after_pending


def render_welcome(template: str, *, mention: str, user: str, guild: str, member_count: int) -> str:
    """Render the welcome template with the standard placeholder set."""
    return render_template(template, mention=mention, user=user, guild=guild, member_count=member_count)


def placeholder_help() -> str:
    """Human-readable list of supported template placeholders."""
    return "\n".join(f"`{{{name}}}` — {description}" for name, description in TEMPLATE_PLACEHOLDERS.items())


def autorole_block_reason(member: discord.Member, role: discord.Role) -> str | None:
    """Why ``member`` may not set up ``role`` as the auto-role, or ``None`` if they may.

    Checks both halves: can ``member`` configure this role (not an escalation above their own rank), and may the
    bot grant it automatically (not dangerous, not above the bot). Both must pass.
    """
    return configure_block_reason(member, role) or grant_block_reason(role)


class WelcomeCog(commands.Cog):
    """Join handling plus the ``/welcome`` admin group."""

    welcome_group = app_commands.Group(
        name="welcome",
        description="Configure the welcome message and auto-role.",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    def __init__(self, bot: SideQuestBot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        """Welcome and auto-role the member now, unless they're still mid-screening."""
        if member.bot or member.pending:
            return
        config = await self.bot.configs.get(member.guild.id)
        await self._welcome(member, config)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        """Run the deferred welcome once a pending member clears membership screening."""
        if after.bot or not completed_screening(before_pending=before.pending, after_pending=after.pending):
            return
        config = await self.bot.configs.get(after.guild.id)
        await self._welcome(after, config)

    async def _welcome(self, member: discord.Member, config: GuildConfigSchema) -> None:
        if config.auto_role_id is not None:
            await self._grant_auto_role(member, config.auto_role_id)
        if config.welcome_channel_id is not None:
            await self._send_welcome(member, config, config.welcome_channel_id)

    async def _grant_auto_role(self, member: discord.Member, role_id: int) -> None:
        role = member.guild.get_role(role_id)
        if role is None:
            return
        if (reason := grant_block_reason(role)) is not None:
            logger.warning("Auto-role %s in guild %s is no longer safe to grant: %s", role_id, member.guild.id, reason)
            return
        try:
            await member.add_roles(role, reason="Auto-role on join")
        except discord.HTTPException:
            logger.warning("Failed to grant auto-role to %s in guild %s", member.id, member.guild.id, exc_info=True)

    async def _send_welcome(self, member: discord.Member, config: GuildConfigSchema, channel_id: int) -> None:
        channel = member.guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        text = render_welcome(
            config.welcome_message,
            mention=member.mention,
            user=member.display_name,
            guild=member.guild.name,
            member_count=member.guild.member_count or 0,
        )
        try:
            await channel.send(text, allowed_mentions=discord.AllowedMentions(users=True))
        except discord.HTTPException:
            logger.warning("Failed to send welcome message in guild %s", member.guild.id, exc_info=True)

    # --- /welcome group -----------------------------------------------------------------------------------------

    @welcome_group.command(name="channel", description="Set (or clear) the welcome channel.")
    @app_commands.describe(channel="Text channel to post welcomes to; omit to disable welcomes.")
    async def welcome_channel(
        self, interaction: discord.Interaction[SideQuestBot], channel: discord.TextChannel | None = None
    ) -> None:
        """Update ``welcome_channel_id``."""
        if interaction.guild is None:
            return
        await interaction.client.configs.update(
            interaction.guild.id, GuildConfigUpdate(welcome_channel_id=channel.id if channel else None)
        )
        message = f"Welcome messages will post to {channel.mention}." if channel else "Welcome messages disabled."
        await interaction.response.send_message(message, ephemeral=True)

    @welcome_group.command(name="message", description="Set the welcome message template.")
    @app_commands.describe(text="Template text; see the placeholder list in the response.")
    async def welcome_message(self, interaction: discord.Interaction[SideQuestBot], text: str) -> None:
        """Update ``welcome_message``."""
        if interaction.guild is None:
            return
        await interaction.client.configs.update(interaction.guild.id, GuildConfigUpdate(welcome_message=text))
        await interaction.response.send_message(
            f"Welcome message updated.\n\n**Placeholders:**\n{placeholder_help()}", ephemeral=True
        )

    @welcome_group.command(name="autorole", description="Set (or clear) the role given to new members.")
    @app_commands.describe(role="Role to grant on join; omit to disable auto-role.")
    async def welcome_autorole(
        self, interaction: discord.Interaction[SideQuestBot], role: discord.Role | None = None
    ) -> None:
        """Update ``auto_role_id`` after checking the invoker may configure it and the bot may grant it."""
        guild = interaction.guild
        if guild is None or not isinstance(interaction.user, discord.Member):
            return
        if role is not None:
            reason = autorole_block_reason(interaction.user, role)
            if reason is not None:
                await interaction.response.send_message(reason, ephemeral=True)
                return
        await interaction.client.configs.update(guild.id, GuildConfigUpdate(auto_role_id=role.id if role else None))
        message = f"New members will get {role.mention}." if role else "Auto-role disabled."
        await interaction.response.send_message(message, ephemeral=True)

    @welcome_group.command(name="test", description="Preview your own welcome message (only visible to you).")
    async def welcome_test(self, interaction: discord.Interaction[SideQuestBot]) -> None:
        """Render the welcome template for the invoker as an ephemeral preview."""
        guild = interaction.guild
        if guild is None:
            return
        config = await interaction.client.configs.get(guild.id)

        text = render_welcome(
            config.welcome_message,
            mention=interaction.user.mention,
            user=interaction.user.display_name,
            guild=guild.name,
            member_count=guild.member_count or 0,
        )
        await interaction.response.send_message(f"Preview:\n\n{text}", ephemeral=True)

    @welcome_group.command(name="status", description="Show the current welcome configuration.")
    async def welcome_status(self, interaction: discord.Interaction[SideQuestBot]) -> None:
        """Report channel, auto-role, and message."""
        if interaction.guild is None:
            return
        config = await interaction.client.configs.get(interaction.guild.id)
        channel_text = f"<#{config.welcome_channel_id}>" if config.welcome_channel_id else "not set"
        role_text = f"<@&{config.auto_role_id}>" if config.auto_role_id else "not set"
        embed = make_embed(title="Welcome configuration")
        embed.add_field(name="Channel", value=channel_text, inline=True)
        embed.add_field(name="Auto-role", value=role_text, inline=True)
        embed.add_field(name="Message", value=config.welcome_message, inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: SideQuestBot) -> None:
    """Register the cog."""
    await bot.add_cog(WelcomeCog(bot))
