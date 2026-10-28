"""Site sign-in and Battle.net link roles, plus the ``/linkroles`` admin group.

Every 60 s: for each guild with either role configured, pull who has signed in and who has also linked Battle.net
from the API, and reconcile every member's roles against that. ``on_member_join``/screening-clear apply the same
plan immediately so a new member doesn't wait up to a minute for their role. The bot is the only thing that talks
to Discord about these roles; the API only reports who is linked. See ``local://link-roles-contract.md`` (read at
implementation time) for the full wire contract.
"""

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands, tasks
from pydantic import ValidationError

from sidequest_bot.api_client import APIError, NotFoundError
from sidequest_bot.bot import SideQuestBot  # noqa: TC001  # discord.py evaluates app-command annotations at runtime
from sidequest_bot.lib.roles import grant_block_reason
from sidequest_bot.plugins.welcome import autorole_block_reason, completed_screening
from sidequest_common.schemas import GuildConfigUpdate

if TYPE_CHECKING:
    from sidequest_common.schemas import GuildConfigSchema, MemberLink

logger = logging.getLogger(__name__)

SYNC_INTERVAL_SECONDS = 60


def plan_role_changes(
    member_role_ids: set[int],
    *,
    linked: bool,
    battlenet_linked: bool,
    linked_role_id: int | None,
    battlenet_role_id: int | None,
) -> tuple[list[int], list[int]]:
    """Which of the two configured roles to add/remove for one member.

    Only ever touches ``linked_role_id``/``battlenet_role_id`` (a ``None`` id is skipped entirely): the site role
    tracks ``linked``, the Battle.net role additionally requires ``battlenet_linked`` (so unlinking Battle.net drops
    just that role, and unlinking the site account drops both).
    """
    to_add: list[int] = []
    to_remove: list[int] = []
    if linked_role_id is not None:
        has_role = linked_role_id in member_role_ids
        if linked and not has_role:
            to_add.append(linked_role_id)
        elif not linked and has_role:
            to_remove.append(linked_role_id)
    if battlenet_role_id is not None:
        wants_role = linked and battlenet_linked
        has_role = battlenet_role_id in member_role_ids
        if wants_role and not has_role:
            to_add.append(battlenet_role_id)
        elif not wants_role and has_role:
            to_remove.append(battlenet_role_id)
    return to_add, to_remove


class LinkRolesCog(commands.Cog):
    """Periodic role sync plus the join/screening listeners and the ``/linkroles`` admin group."""

    linkroles_group = app_commands.Group(
        name="linkroles",
        description="Configure the roles granted for signing in on the site and linking Battle.net.",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    def __init__(self, bot: SideQuestBot) -> None:
        self.bot = bot
        self._not_site_guild_ids: set[int] = set()
        self._warned_role_ids: set[tuple[int, int]] = set()
        self._sync_loop.start()

    async def cog_unload(self) -> None:
        """Stop the sync loop when the cog is unloaded."""
        self._sync_loop.cancel()

    # --- Periodic sync ----------------------------------------------------------------------------------------

    @tasks.loop(seconds=SYNC_INTERVAL_SECONDS)
    async def _sync_loop(self) -> None:
        for guild in list(self.bot.guilds):
            if guild.id in self._not_site_guild_ids:
                continue
            try:
                await self._sync_guild(guild)
            except NotFoundError:
                logger.info("Guild %s is not the site guild; no longer syncing link roles there", guild.id)
                self._not_site_guild_ids.add(guild.id)
            except APIError, ValidationError:
                # A failure here must not escape: an unhandled exception stops a tasks.loop for good. A response
                # that doesn't match the schema (e.g. an API still on the previous release) is retried next tick.
                logger.warning("Link role sync failed for guild %s", guild.id, exc_info=True)

    @_sync_loop.before_loop
    async def _before_sync_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def _sync_guild(self, guild: discord.Guild) -> None:
        config = await self.bot.configs.get(guild.id)
        if config.linked_role_id is None and config.battlenet_role_id is None:
            return
        battlenet_by_discord_id = await self._fetch_links(guild.id)
        for member in guild.members:
            if member.bot:
                continue
            await self._apply_member(guild, member, config, battlenet_by_discord_id)

    async def _fetch_links(self, guild_id: int) -> dict[int, bool]:
        links: list[MemberLink] = await self.bot.api.list_member_links(guild_id)
        return {link.discord_id: link.battlenet_linked for link in links}

    async def _apply_member(
        self,
        guild: discord.Guild,
        member: discord.Member,
        config: GuildConfigSchema,
        battlenet_by_discord_id: dict[int, bool],
    ) -> None:
        linked = member.id in battlenet_by_discord_id
        battlenet_linked = battlenet_by_discord_id.get(member.id, False)
        member_role_ids = {role.id for role in member.roles}
        to_add, to_remove = plan_role_changes(
            member_role_ids,
            linked=linked,
            battlenet_linked=battlenet_linked,
            linked_role_id=config.linked_role_id,
            battlenet_role_id=config.battlenet_role_id,
        )
        if not to_add and not to_remove:
            return
        for role_id in to_add:
            if (role := self._resolve_grantable_role(guild, role_id)) is None:
                continue
            reason = "Signed in on sidequest-guild.com" if role_id == config.linked_role_id else "Linked Battle.net"
            try:
                await member.add_roles(role, reason=reason)
            except discord.HTTPException:
                logger.warning(
                    "Failed to grant link role %s to %s in guild %s", role_id, member.id, guild.id, exc_info=True
                )
        remove_roles = [role for role_id in to_remove if (role := guild.get_role(role_id)) is not None]
        if remove_roles:
            try:
                await member.remove_roles(*remove_roles, reason="Unlinked")
            except discord.HTTPException:
                logger.warning("Failed to remove link role(s) from %s in guild %s", member.id, guild.id, exc_info=True)

    def _resolve_grantable_role(self, guild: discord.Guild, role_id: int) -> discord.Role | None:
        """``role_id``'s role if the bot may grant it, warning once per (guild, role) otherwise."""
        key = (guild.id, role_id)
        role = guild.get_role(role_id)
        if role is None:
            if key not in self._warned_role_ids:
                logger.warning("Configured link role %s in guild %s no longer exists", role_id, guild.id)
                self._warned_role_ids.add(key)
            return None
        if (reason := grant_block_reason(role)) is not None:
            if key not in self._warned_role_ids:
                logger.warning("Link role %s in guild %s is no longer safe to grant: %s", role_id, guild.id, reason)
                self._warned_role_ids.add(key)
            return None
        self._warned_role_ids.discard(key)
        return role

    # --- Join / screening listeners ---------------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        """Apply the member's plan right away, unless they're still mid-screening."""
        if member.bot or member.pending:
            return
        await self._sync_member_now(member)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        """Apply the deferred plan once a pending member clears membership screening."""
        if after.bot or not completed_screening(before_pending=before.pending, after_pending=after.pending):
            return
        await self._sync_member_now(after)

    async def _sync_member_now(self, member: discord.Member) -> None:
        guild = member.guild
        if guild.id in self._not_site_guild_ids:
            return
        try:
            config = await self.bot.configs.get(guild.id)
            if config.linked_role_id is None and config.battlenet_role_id is None:
                return
            battlenet_by_discord_id = await self._fetch_links(guild.id)
        except NotFoundError:
            self._not_site_guild_ids.add(guild.id)
            return
        except APIError:
            logger.warning("Link role sync failed for %s in guild %s", member.id, guild.id, exc_info=True)
            return
        await self._apply_member(guild, member, config, battlenet_by_discord_id)

    # --- /linkroles group ---------------------------------------------------------------------------------------

    @linkroles_group.command(name="site", description="Set (or clear) the role granted for signing in on the site.")
    @app_commands.describe(role="Role to grant on sign-in; omit to disable.")
    async def linkroles_site(
        self, interaction: discord.Interaction[SideQuestBot], role: discord.Role | None = None
    ) -> None:
        """Update ``linked_role_id`` after checking the invoker may configure it and the bot may grant it."""
        guild = interaction.guild
        if guild is None or not isinstance(interaction.user, discord.Member):
            return
        if role is not None:
            reason = autorole_block_reason(interaction.user, role)
            if reason is not None:
                await interaction.response.send_message(reason, ephemeral=True)
                return
        await interaction.client.configs.update(guild.id, GuildConfigUpdate(linked_role_id=role.id if role else None))
        message = f"Members who sign in will get {role.mention}." if role else "Site link role disabled."
        await interaction.response.send_message(message, ephemeral=True)

    @linkroles_group.command(name="battlenet", description="Set (or clear) the role granted for linking Battle.net.")
    @app_commands.describe(role="Role to grant for linking Battle.net; omit to disable.")
    async def linkroles_battlenet(
        self, interaction: discord.Interaction[SideQuestBot], role: discord.Role | None = None
    ) -> None:
        """Update ``battlenet_role_id`` after checking the invoker may configure it and the bot may grant it."""
        guild = interaction.guild
        if guild is None or not isinstance(interaction.user, discord.Member):
            return
        if role is not None:
            reason = autorole_block_reason(interaction.user, role)
            if reason is not None:
                await interaction.response.send_message(reason, ephemeral=True)
                return
        await interaction.client.configs.update(
            guild.id, GuildConfigUpdate(battlenet_role_id=role.id if role else None)
        )
        message = f"Members who link Battle.net will get {role.mention}." if role else "Battle.net link role disabled."
        await interaction.response.send_message(message, ephemeral=True)


async def setup(bot: SideQuestBot) -> None:
    """Register the cog."""
    await bot.add_cog(LinkRolesCog(bot))
