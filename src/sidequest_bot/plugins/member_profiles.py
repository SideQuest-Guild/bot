"""Continuous Discord profile sync for member rows: names, nick, avatar and roles, not just at sign-in.

On ``on_ready`` and every 10 minutes: pull the linked-member set from ``GET .../member-links``, push the current
profile for every linked member still in the guild, and report linked members no longer in the guild as departed.
Also pushes immediately, debounced ~5s per guild and coalescing ids, on nick/role/guild-avatar changes
(``on_member_update``), username/global-name/avatar changes (``on_user_update``, checked against every site guild
the user shares with the bot), joins (``on_member_join``), and departures (``on_member_remove``). Only linked
members are ever pushed — ids outside the member-links set are skipped; the periodic sweep keeps that set current.
See ``PUT /api/guilds/{guild_id}/member-profiles``.
"""

import asyncio
import logging

import discord  # noqa: TC002  # annotations evaluated eagerly (no future import); used throughout this module
from discord.ext import commands, tasks

from sidequest_bot.api_client import APIError, NotFoundError
from sidequest_bot.bot import SideQuestBot  # noqa: TC001  # discord.py evaluates app-command annotations at runtime
from sidequest_common.schemas import MemberProfileIn

logger = logging.getLogger(__name__)

SWEEP_INTERVAL_SECONDS = 600
DEBOUNCE_SECONDS = 5


def build_member_profile(member: discord.Member) -> MemberProfileIn:
    """A member's current Discord profile, matching the fields ``upsert_from_discord`` sets at sign-in.

    ``avatar_hash`` is the member's main (non-guild) avatar, matching the sign-in payload's ``user["avatar"]``.
    """
    return MemberProfileIn(
        discord_id=member.id,
        username=member.name,
        global_name=member.global_name,
        guild_nick=member.nick,
        avatar_hash=member.avatar.key if member.avatar is not None else None,
        roles=[role.id for role in member.roles if role.id != member.guild.id],
    )


def build_sweep(guild: discord.Guild, linked_ids: set[int]) -> tuple[list[MemberProfileIn], list[int]]:
    """Profiles for every linked member currently in ``guild``, and the sorted ids of linked members who've left."""
    profiles: list[MemberProfileIn] = []
    present_ids: set[int] = set()
    for discord_id in linked_ids:
        member = guild.get_member(discord_id)
        if member is None:
            continue
        present_ids.add(discord_id)
        profiles.append(build_member_profile(member))
    return profiles, sorted(linked_ids - present_ids)


def plan_pending_push(
    guild: discord.Guild, pending_ids: set[int], linked_ids: set[int]
) -> tuple[list[MemberProfileIn], list[int]]:
    """Of ``pending_ids``, profiles for those still in ``guild`` and departed ids for those who left.

    Ids not in ``linked_ids`` (never signed in, or not yet picked up by the periodic sweep) are skipped entirely.
    """
    profiles: list[MemberProfileIn] = []
    departed_ids: list[int] = []
    for discord_id in pending_ids:
        if discord_id not in linked_ids:
            continue
        member = guild.get_member(discord_id)
        if member is None:
            departed_ids.append(discord_id)
        else:
            profiles.append(build_member_profile(member))
    return profiles, sorted(departed_ids)


class MemberProfilesCog(commands.Cog):
    """Periodic full sweep plus debounced, per-guild, id-coalescing pushes on Discord profile changes."""

    def __init__(self, bot: SideQuestBot) -> None:
        self.bot = bot
        self._not_site_guild_ids: set[int] = set()
        self._linked_ids: dict[int, set[int]] = {}
        self._pending_ids: dict[int, set[int]] = {}
        self._debounce_tasks: dict[int, asyncio.Task[None]] = {}
        self._sweep_loop.start()

    async def cog_unload(self) -> None:
        """Stop the sweep loop and any pending debounced pushes."""
        self._sweep_loop.cancel()
        for task in self._debounce_tasks.values():
            task.cancel()

    # --- Periodic sweep ------------------------------------------------------------------------------------------

    @tasks.loop(seconds=SWEEP_INTERVAL_SECONDS)
    async def _sweep_loop(self) -> None:
        for guild in list(self.bot.guilds):
            if guild.id in self._not_site_guild_ids:
                continue
            await self._sweep(guild)

    @_sweep_loop.before_loop
    async def _before_sweep_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def _sweep(self, guild: discord.Guild) -> None:
        """Refresh the linked-member set from the API, then push every linked member's current state."""
        try:
            links = await self.bot.api.list_member_links(guild.id)
        except NotFoundError:
            logger.info("Guild %s is not the site guild; no longer syncing member profiles there", guild.id)
            self._not_site_guild_ids.add(guild.id)
            return
        except APIError:
            logger.warning("Failed to list member links for guild %s", guild.id, exc_info=True)
            return
        linked_ids = {link.discord_id for link in links}
        self._linked_ids[guild.id] = linked_ids
        profiles, departed_ids = build_sweep(guild, linked_ids)
        await self._push(guild, profiles, departed_ids)

    async def _push(self, guild: discord.Guild, profiles: list[MemberProfileIn], departed_ids: list[int]) -> None:
        if not profiles and not departed_ids:
            return
        try:
            await self.bot.api.put_member_profiles(guild.id, profiles, departed_ids)
        except APIError:
            # A 404 here isn't "not the site guild" (member-links already answered for it): it's an API from before
            # this route, mid rolling deploy. Warn and let the next push or sweep retry.
            logger.warning("Failed to push member profiles for guild %s", guild.id, exc_info=True)

    # --- Debounced event-driven pushes ----------------------------------------------------------------------------

    def _schedule(self, guild: discord.Guild, discord_id: int) -> None:
        """Queue ``discord_id`` for a push ~5s from now in ``guild``; ids that arrive meanwhile ride along.

        A pending push is never cancelled, so a push already sending can't be aborted with its ids dropped.
        """
        if guild.id in self._not_site_guild_ids:
            return
        self._pending_ids.setdefault(guild.id, set()).add(discord_id)
        existing = self._debounce_tasks.get(guild.id)
        if existing is None or existing.done():
            self._debounce_tasks[guild.id] = asyncio.ensure_future(self._debounced_push(guild))

    async def _debounced_push(self, guild: discord.Guild) -> None:
        await asyncio.sleep(DEBOUNCE_SECONDS)
        pending_ids = self._pending_ids.pop(guild.id, set())
        if not pending_ids:
            return
        linked_ids = self._linked_ids.get(guild.id, set())
        profiles, departed_ids = plan_pending_push(guild, pending_ids, linked_ids)
        await self._push(guild, profiles, departed_ids)

    # --- Listeners -------------------------------------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        """Nick, role, or guild-avatar change: schedule a debounced push."""
        if before.nick == after.nick and before.roles == after.roles and before.guild_avatar == after.guild_avatar:
            return
        self._schedule(after.guild, after.id)

    @commands.Cog.listener()
    async def on_user_update(self, before: discord.User, after: discord.User) -> None:
        """Username, global name, or avatar change: schedule a debounced push in every site guild they're in."""
        if before.name == after.name and before.global_name == after.global_name and before.avatar == after.avatar:
            return
        for guild in self.bot.guilds:
            if guild.id in self._not_site_guild_ids:
                continue
            if guild.get_member(after.id) is not None:
                self._schedule(guild, after.id)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        """Schedule a debounced push; a no-op push until the member is linked (has signed in)."""
        self._schedule(member.guild, member.id)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        """Schedule a debounced push so a linked member's departure is reported promptly."""
        self._schedule(member.guild, member.id)


async def setup(bot: SideQuestBot) -> None:
    """Register the cog."""
    await bot.add_cog(MemberProfilesCog(bot))
