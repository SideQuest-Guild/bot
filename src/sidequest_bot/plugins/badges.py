"""Hall of Heroes badge roles and announcements, plus the ``/badges`` admin group.

Every 5 minutes (and once on ready, like every other poll loop): ``GET /api/guilds/{guild_id}/badges/sync``
recomputes every member's badges server-side and reports two things, ``BadgeSyncReport.role_state`` (each linked
member's full current badge id set) and ``BadgeSyncReport.pending`` (newly earned badges not yet announced). For
each :data:`sidequest_common.schemas.badges.BADGE_LIST` entry this plugin ensures a matching Discord role exists
(creating or recreating it as needed, storing its id in ``GuildConfig.badge_role_ids``), diffs ``role_state``
against each member's actual roles to add/remove exactly the badge roles that changed, then posts a gold embed for
each pending announcement in ``GuildConfig.badge_channel_id`` and acks it
(``POST /api/guilds/{guild_id}/badges/ack``) so it isn't announced again.

Role membership is driven entirely by ``role_state``, which already reflects the API's rules: permanent badges are
never removed once earned, and Top Helper is a single moving title re-pointed to a new winner server-side, so the
plain "roles must match ``badge_ids``" diff here naturally adds it for the new winner and removes it from the old
one without any special-casing. Only the badge roles this plugin created are ever added or removed; every other
role on a member, and every other role in the guild, is left untouched.

``PendingBadgeAnnouncement`` carries no ``member_id`` (only ``discord_id``), so announcements link to the general
Hall of Heroes page rather than a specific profile. If ``badge_channel_id`` is unset, role sync still runs every
tick but pending badges are left un-acked, so they announce in full once a channel is configured instead of being
silently lost for whoever earned them first.
"""

import logging
from functools import lru_cache
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands, tasks

from sidequest_bot.api_client import APIError, NotFoundError
from sidequest_bot.bot import SideQuestBot  # noqa: TC001  # discord.py evaluates app-command annotations at runtime
from sidequest_bot.lib.embeds import Colors, make_embed
from sidequest_bot.lib.roles import grant_block_reason
from sidequest_common.schemas import BadgeAck, GuildConfigUpdate
from sidequest_common.schemas.badges import BADGE_LIST, BADGES, BadgeDefinition
from sidequest_common.settings import WebSettings

if TYPE_CHECKING:
    from sidequest_common.schemas import BadgeRoleState, GuildConfigSchema, PendingBadgeAnnouncement

logger = logging.getLogger(__name__)

SYNC_INTERVAL_SECONDS = 300


@lru_cache(maxsize=1)
def _web_settings() -> WebSettings:
    """``WebSettings`` (``WEB_*`` env vars), shared with the API: just ``BASE_URL`` here."""
    return WebSettings()


def hall_of_heroes_url(base_url: str) -> str:
    """Link to the Hall of Heroes landing page (no per-member link: announcements carry no ``member_id``)."""
    return f"{base_url.rstrip('/')}/heroes/"


def plan_badge_role_changes(
    member_role_ids: set[int], wanted_badge_ids: set[str], badge_role_ids: dict[str, int]
) -> tuple[list[int], list[int]]:
    """Which of ``badge_role_ids``' roles to add/remove so a member's roles match ``wanted_badge_ids``.

    Only ever touches ids present in ``badge_role_ids``: every other role a member holds is left alone.
    """
    to_add: list[int] = []
    to_remove: list[int] = []
    for badge_id, role_id in badge_role_ids.items():
        has_role = role_id in member_role_ids
        wants_role = badge_id in wanted_badge_ids
        if wants_role and not has_role:
            to_add.append(role_id)
        elif not wants_role and has_role:
            to_remove.append(role_id)
    return to_add, to_remove


def badge_grant_reason(badge_id: str) -> str:
    """Audit-log reason for granting ``badge_id``'s role."""
    badge = BADGES.get(badge_id)
    name = badge.name if badge is not None else badge_id
    return f"Earned {name} on sidequest-guild.com"


def build_badge_embed(announcement: PendingBadgeAnnouncement, profile_url: str) -> discord.Embed:
    """Gold embed for one newly earned badge, with a link to the Hall of Heroes."""
    badge = BADGES.get(announcement.badge_id)
    icon = f"{badge.icon} " if badge is not None else ""
    embed = make_embed(
        f"{icon}Badge earned!",
        f"<@{announcement.discord_id}> earned **{announcement.name}**: {announcement.description}",
        color=Colors.GOLD,
    )
    embed.add_field(name="Hall of Heroes", value=f"[View the Hall of Heroes]({profile_url})", inline=False)
    return embed


async def create_badge_role(guild: discord.Guild, badge: BadgeDefinition) -> discord.Role | None:
    """Create ``badge``'s Discord role: not hoisted, not mentionable, colored per the badge definition.

    Discord places a freshly created role at the bottom of the hierarchy, just above ``@everyone`` and below
    every existing role including the bot's own - exactly "below the bot's top role, above @everyone" - without
    this ever calling ``edit_role_positions`` or touching any other role.
    """
    try:
        return await guild.create_role(
            name=badge.name,
            colour=discord.Colour(badge.color),
            hoist=False,
            mentionable=False,
            reason=f"Hall of Heroes badge role: {badge.name}",
        )
    except discord.HTTPException:
        logger.warning("Failed to create badge role %s in guild %s", badge.id, guild.id, exc_info=True)
        return None


class BadgesCog(commands.Cog):
    """Periodic badge role sync + announcements, plus the ``/badges`` admin group."""

    badges_group = app_commands.Group(
        name="badges",
        description="Configure Hall of Heroes badge announcements and run a sync now.",
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
                # /badges/sync has no established guild-identity check to confirm this guild really isn't the
                # site guild (unlike link_roles' list_member_links), so a 404 here is just as likely a rolling
                # deploy hitting an old API pod that doesn't have the route yet. Never exclude; retry next tick.
                logger.info("Badge sync endpoint not available yet for guild %s; retrying next tick", guild.id)
            except APIError:
                # Must not escape: an unhandled exception stops a tasks.loop for good. Retried next tick.
                logger.warning("Badge sync failed for guild %s", guild.id, exc_info=True)

    @_sync_loop.before_loop
    async def _before_sync_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def _sync_guild(self, guild: discord.Guild) -> tuple[int, int]:
        """Ensure roles, reconcile every linked member's badge roles, then announce + ack pending badges.

        Returns ``(members with badge state, badges announced)`` for the ``/badges sync`` ephemeral summary.
        """
        config = await self.bot.configs.get(guild.id)
        report = await self.bot.api.sync_badges(guild.id)
        badge_role_ids = await self._ensure_badge_roles(guild, config)
        await self._apply_role_state(guild, badge_role_ids, report.role_state)
        announced = await self._announce_pending(guild, config, report.pending)
        return len(report.role_state), announced

    async def _ensure_badge_roles(self, guild: discord.Guild, config: GuildConfigSchema) -> dict[str, int]:
        """Create any missing or deleted badge role, persist the updated map, and return it."""
        updated = dict(config.badge_role_ids)
        changed = False
        for badge in BADGE_LIST:
            stored_id = updated.get(badge.id)
            if stored_id is not None and guild.get_role(stored_id) is not None:
                continue
            new_role = await create_badge_role(guild, badge)
            if new_role is None:
                if updated.pop(badge.id, None) is not None:
                    changed = True
                continue
            updated[badge.id] = new_role.id
            changed = True
        if changed:
            await self.bot.configs.update(guild.id, GuildConfigUpdate(badge_role_ids=updated))
        return updated

    async def _apply_role_state(
        self, guild: discord.Guild, badge_role_ids: dict[str, int], role_state: list[BadgeRoleState]
    ) -> None:
        for state in role_state:
            member = guild.get_member(state.discord_id)
            if member is None:
                continue
            member_role_ids = {role.id for role in member.roles}
            to_add, to_remove = plan_badge_role_changes(member_role_ids, set(state.badge_ids), badge_role_ids)
            if not to_add and not to_remove:
                continue
            await self._apply_member_roles(guild, member, to_add, to_remove, badge_role_ids)

    async def _apply_member_roles(
        self,
        guild: discord.Guild,
        member: discord.Member,
        to_add: list[int],
        to_remove: list[int],
        badge_role_ids: dict[str, int],
    ) -> None:
        id_to_badge_id = {role_id: badge_id for badge_id, role_id in badge_role_ids.items()}
        for role_id in to_add:
            role = self._resolve_grantable_role(guild, role_id)
            if role is None:
                continue
            try:
                await member.add_roles(role, reason=badge_grant_reason(id_to_badge_id.get(role_id, "")))
            except discord.HTTPException:
                logger.warning(
                    "Failed to grant badge role %s to %s in guild %s", role_id, member.id, guild.id, exc_info=True
                )
        remove_roles = [role for role_id in to_remove if (role := guild.get_role(role_id)) is not None]
        if remove_roles:
            try:
                await member.remove_roles(*remove_roles, reason="No longer holds this Hall of Heroes badge")
            except discord.HTTPException:
                logger.warning("Failed to remove badge role(s) from %s in guild %s", member.id, guild.id, exc_info=True)

    def _resolve_grantable_role(self, guild: discord.Guild, role_id: int) -> discord.Role | None:
        """``role_id``'s role if the bot may grant it, warning once per (guild, role) otherwise."""
        key = (guild.id, role_id)
        role = guild.get_role(role_id)
        if role is None:
            return None
        if (reason := grant_block_reason(role)) is not None:
            if key not in self._warned_role_ids:
                logger.warning("Badge role %s in guild %s is no longer safe to grant: %s", role_id, guild.id, reason)
                self._warned_role_ids.add(key)
            return None
        self._warned_role_ids.discard(key)
        return role

    async def _announce_pending(
        self, guild: discord.Guild, config: GuildConfigSchema, pending: list[PendingBadgeAnnouncement]
    ) -> int:
        if not pending or config.badge_channel_id is None:
            return 0
        channel = guild.get_channel(config.badge_channel_id)
        if not isinstance(channel, discord.TextChannel):
            return 0
        announced = 0
        for announcement in pending:
            if await self._announce_one(guild, channel, announcement):
                announced += 1
        return announced

    async def _announce_one(
        self, guild: discord.Guild, channel: discord.TextChannel, announcement: PendingBadgeAnnouncement
    ) -> bool:
        embed = build_badge_embed(announcement, hall_of_heroes_url(_web_settings().BASE_URL))
        mention_target = guild.get_member(announcement.discord_id) or discord.Object(id=announcement.discord_id)
        try:
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions(users=[mention_target]))
        except discord.HTTPException:
            logger.warning(
                "Failed to post badge announcement for %s in guild %s",
                announcement.discord_id,
                guild.id,
                exc_info=True,
            )
            return False
        try:
            await self.bot.api.ack_badge(
                guild.id, BadgeAck(discord_id=announcement.discord_id, badge_id=announcement.badge_id)
            )
        except APIError:
            logger.warning(
                "Failed to ack badge announcement for %s in guild %s", announcement.discord_id, guild.id, exc_info=True
            )
        return True

    # --- /badges group -------------------------------------------------------------------------------------------

    @badges_group.command(
        name="channel", description="Set (or clear) the channel for Hall of Heroes badge announcements."
    )
    @app_commands.describe(channel="Channel to post badge announcements in; omit to disable.")
    async def badges_channel(
        self, interaction: discord.Interaction[SideQuestBot], channel: discord.TextChannel | None = None
    ) -> None:
        """Update ``badge_channel_id``. Role sync keeps running even while this is unset."""
        guild = interaction.guild
        if guild is None:
            return
        await interaction.client.configs.update(
            guild.id, GuildConfigUpdate(badge_channel_id=channel.id if channel else None)
        )
        message = (
            f"Badge announcements will post in {channel.mention}."
            if channel
            else "Badge announcements disabled (role sync still runs)."
        )
        await interaction.response.send_message(message, ephemeral=True)

    @badges_group.command(name="sync", description="Sync Hall of Heroes badge roles and announcements now.")
    async def badges_sync(self, interaction: discord.Interaction[SideQuestBot]) -> None:
        """Run a sync immediately instead of waiting for the next periodic tick, with an ephemeral summary."""
        guild = interaction.guild
        if guild is None:
            return
        await interaction.response.defer(ephemeral=True)
        try:
            member_count, announced = await self._sync_guild(guild)
        except APIError:
            # Includes NotFoundError: /badges/sync has no reliable "wrong guild" signal (see _sync_loop), so a
            # 404 here is treated the same as any other transient failure.
            logger.warning("Manual badge sync failed for guild %s", guild.id, exc_info=True)
            await interaction.followup.send("Couldn't reach the guild records; try again shortly.", ephemeral=True)
            return
        await interaction.followup.send(
            f"Synced badge roles for {member_count} member(s); announced {announced} new badge(s).", ephemeral=True
        )


async def setup(bot: SideQuestBot) -> None:
    """Register the cog."""
    await bot.add_cog(BadgesCog(bot))
