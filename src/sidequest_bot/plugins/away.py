"""Grants/removes the "Away on a Quest" role for members whose away range (``/account/``) covers today.

The bot owns ``GuildConfig.away_role_id`` fully for every linked member: nobody else grants this role, so the
sync loop can add and remove it freely without conflicting with any other grant. The away-now endpoint has no
established guild-identity check to confirm a guild really isn't the site guild, so a 404 is just as likely a
rolling deploy hitting an old API pod that doesn't have the route yet: it's never treated as permanent, and the
guild is retried on the next poll.
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

from sidequest_bot.api_client import APIError, NotFoundError
from sidequest_bot.bot import SideQuestBot  # noqa: TC001  # discord.py evaluates app-command annotations at runtime
from sidequest_bot.lib.roles import configure_block_reason, grant_block_reason
from sidequest_common.schemas import GuildConfigUpdate

logger = logging.getLogger(__name__)

SYNC_INTERVAL_SECONDS = 60


def plan_away_role(*, away_now: bool, has_role: bool) -> tuple[bool, bool]:
    """Whether to add/remove the away role for one member. Pure: ``(to_add, to_remove)``."""
    if away_now and not has_role:
        return True, False
    if not away_now and has_role:
        return False, True
    return False, False


class AwayCog(commands.Cog):
    """Periodic away-role sync plus the ``/away role`` admin command."""

    away_group = app_commands.Group(
        name="away",
        description="Configure the role granted while a member is marked away on the site.",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    def __init__(self, bot: SideQuestBot) -> None:
        self.bot = bot
        self._warned_role_ids: set[tuple[int, int]] = set()
        self._sync_loop.start()

    async def cog_unload(self) -> None:
        """Stop the sync loop when the cog is unloaded."""
        self._sync_loop.cancel()

    # --- Periodic sync ----------------------------------------------------------------------------------------

    @tasks.loop(seconds=SYNC_INTERVAL_SECONDS)
    async def _sync_loop(self) -> None:
        for guild in list(self.bot.guilds):
            try:
                await self._sync_guild(guild)
            except NotFoundError:
                # No reliable "wrong guild" signal here (see the module docstring); never exclude, just retry.
                logger.info("Away role endpoint not available yet for guild %s; retrying next tick", guild.id)
            except APIError:
                # Must not escape: an unhandled exception stops a tasks.loop for good. Retried next tick.
                logger.warning("Away role sync failed for guild %s", guild.id, exc_info=True)

    @_sync_loop.before_loop
    async def _before_sync_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def _sync_guild(self, guild: discord.Guild) -> None:
        config = await self.bot.configs.get(guild.id)
        if config.away_role_id is None:
            return
        role = self._resolve_grantable_role(guild, config.away_role_id)
        if role is None:
            return
        away_discord_ids = set(await self.bot.api.get_away_now(guild.id))
        for member in guild.members:
            if member.bot:
                continue
            await self._apply_member(member, role, away_now=member.id in away_discord_ids)

    async def _apply_member(self, member: discord.Member, role: discord.Role, *, away_now: bool) -> None:
        has_role = role.id in {r.id for r in member.roles}
        to_add, to_remove = plan_away_role(away_now=away_now, has_role=has_role)
        if not to_add and not to_remove:
            return
        try:
            if to_add:
                await member.add_roles(role, reason="Marked away on sidequest-guild.com")
            else:
                await member.remove_roles(role, reason="No longer marked away on sidequest-guild.com")
        except discord.HTTPException:
            logger.warning(
                "Failed to update the away role for %s in guild %s", member.id, member.guild.id, exc_info=True
            )

    def _resolve_grantable_role(self, guild: discord.Guild, role_id: int) -> discord.Role | None:
        """``role_id``'s role if the bot may grant it, warning once per (guild, role) otherwise."""
        key = (guild.id, role_id)
        role = guild.get_role(role_id)
        if role is None:
            if key not in self._warned_role_ids:
                logger.warning("Configured away role %s in guild %s no longer exists", role_id, guild.id)
                self._warned_role_ids.add(key)
            return None
        if (reason := grant_block_reason(role)) is not None:
            if key not in self._warned_role_ids:
                logger.warning("Away role %s in guild %s is no longer safe to grant: %s", role_id, guild.id, reason)
                self._warned_role_ids.add(key)
            return None
        self._warned_role_ids.discard(key)
        return role

    # --- /away group ---------------------------------------------------------------------------------------

    @away_group.command(name="role", description="Set (or clear) the role granted while a member is marked away.")
    @app_commands.describe(role="Role to grant while a member is marked away; omit to disable.")
    async def away_role(self, interaction: discord.Interaction[SideQuestBot], role: discord.Role | None = None) -> None:
        """Update ``away_role_id`` after checking the invoker may configure it and the bot may grant it."""
        guild = interaction.guild
        if guild is None or not isinstance(interaction.user, discord.Member):
            return
        if role is not None:
            reason = configure_block_reason(interaction.user, role)
            if reason is not None:
                await interaction.response.send_message(reason, ephemeral=True)
                return
        await interaction.client.configs.update(guild.id, GuildConfigUpdate(away_role_id=role.id if role else None))
        message = f"Members marked away will get {role.mention}." if role else "Away role disabled."
        await interaction.response.send_message(message, ephemeral=True)


async def setup(bot: SideQuestBot) -> None:
    """Register the cog."""
    await bot.add_cog(AwayCog(bot))
