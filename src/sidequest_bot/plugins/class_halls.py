"""Class halls: a forum post per role that follows the role's members into it.

A thread named ``"<role name> Hall"`` in any forum channel is that role's hall (e.g. ``Warrior Hall`` for
``@Warrior``). Gaining the role adds the member to the hall's thread, so it shows up in their channel list; losing
the role takes them out again. Every hall stays open for anyone who can see the forum to browse and follow. No
configuration: renaming a role or its post is all it takes to pair or unpair them.

A sweep at startup and every few hours adds role holders the bot missed while offline. It never removes anyone:
people follow halls on their own (or by posting in them), and that's theirs to keep.
"""

import logging
from typing import Final

import discord
from discord.ext import commands, tasks

from sidequest_bot.bot import SideQuestBot  # noqa: TC001  # discord.py evaluates app-command annotations at runtime

logger = logging.getLogger(__name__)

HALL_SUFFIX: Final = " Hall"
SWEEP_INTERVAL_HOURS: Final = 6


def hall_name(role_name: str) -> str:
    """The forum post name that marks ``role_name``'s hall."""
    return f"{role_name}{HALL_SUFFIX}"


def role_changes(before: set[int], after: set[int]) -> tuple[set[int], set[int]]:
    """Role ids gained and lost between two snapshots of a member's roles."""
    return after - before, before - after


def missing_followers(role_holders: set[int], hall_members: set[int]) -> set[int]:
    """Role holders who aren't following their hall yet."""
    return role_holders - hall_members


class ClassHallsCog(commands.Cog):
    """Keeps each role's hall thread membership in step with who holds the role."""

    def __init__(self, bot: SideQuestBot) -> None:
        self.bot = bot
        self._hall_ids: dict[tuple[int, str], int] = {}
        """(guild id, hall name) -> thread id; rebuilt by every sweep, extended by ``on_thread_create``."""
        self._sweep_loop.start()

    async def cog_unload(self) -> None:
        """Stop the periodic sweep."""
        self._sweep_loop.cancel()

    # --- Finding halls ---------------------------------------------------------------------------------------

    async def _hall_for(self, guild: discord.Guild, role: discord.Role) -> discord.Thread | None:
        """``role``'s hall from the index the sweep builds (no forum scan on every role change)."""
        thread_id = self._hall_ids.get((guild.id, hall_name(role.name)))
        if thread_id is None:
            return None
        if (thread := guild.get_thread(thread_id)) is not None:
            return thread
        try:
            channel = await guild.fetch_channel(thread_id)
        except discord.NotFound:
            self._hall_ids.pop((guild.id, hall_name(role.name)), None)
            return None
        except discord.HTTPException:
            logger.warning("Couldn't fetch hall %s", thread_id, exc_info=True)
            return None
        return channel if isinstance(channel, discord.Thread) else None

    @commands.Cog.listener()
    async def on_thread_create(self, thread: discord.Thread) -> None:
        """Index a new forum post named like a hall right away, rather than at the next sweep."""
        if thread.name.endswith(HALL_SUFFIX) and isinstance(thread.parent, discord.ForumChannel):
            self._hall_ids[(thread.guild.id, thread.name)] = thread.id

    @staticmethod
    async def _open(thread: discord.Thread) -> bool:
        """Unarchive ``thread`` if needed (archived threads can't take new members). False if that failed."""
        if not thread.archived:
            return True
        try:
            await thread.edit(archived=False)
        except discord.HTTPException:
            logger.warning("Couldn't unarchive hall %s", thread.id, exc_info=True)
            return False
        return True

    # --- Role changes ----------------------------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        """Follow ``after`` into the halls of roles they gained, and out of the ones they lost."""
        if after.bot:
            return
        gained, lost = role_changes({role.id for role in before.roles}, {role.id for role in after.roles})
        for role_id in gained | lost:
            role = after.guild.get_role(role_id)
            if role is None or (hall := await self._hall_for(after.guild, role)) is None:
                continue
            await self._set_following(hall, after, following=role_id in gained)

    async def _set_following(self, hall: discord.Thread, member: discord.Member, *, following: bool) -> None:
        try:
            if following:
                if await self._open(hall):
                    await hall.add_user(member)
            else:
                await hall.remove_user(member)
        except discord.HTTPException:
            action = "add" if following else "remove"
            logger.warning("Couldn't %s %s in hall %s", action, member.id, hall.id, exc_info=True)

    # --- Periodic sweep --------------------------------------------------------------------------------------

    @tasks.loop(hours=SWEEP_INTERVAL_HOURS)
    async def _sweep_loop(self) -> None:
        for guild in list(self.bot.guilds):
            halls = await self._all_halls(guild)
            for key in [key for key in self._hall_ids if key[0] == guild.id]:
                del self._hall_ids[key]
            self._hall_ids.update({(guild.id, name): thread.id for name, thread in halls.items()})
            for role in guild.roles:
                if (hall := halls.get(hall_name(role.name))) is not None:
                    await self._sweep_hall(guild, role, hall)

    @_sweep_loop.before_loop
    async def _before_sweep_loop(self) -> None:
        await self.bot.wait_until_ready()

    @staticmethod
    async def _all_halls(guild: discord.Guild) -> dict[str, discord.Thread]:
        """Every forum post named like a hall, active or archived, by name."""
        halls = {
            thread.name: thread
            for thread in guild.threads
            if thread.name.endswith(HALL_SUFFIX) and isinstance(thread.parent, discord.ForumChannel)
        }
        for forum in guild.forums:
            try:
                async for thread in forum.archived_threads(limit=None):
                    if thread.name.endswith(HALL_SUFFIX):
                        halls.setdefault(thread.name, thread)
            except discord.HTTPException:
                logger.warning("Couldn't list archived posts in forum %s", forum.id, exc_info=True)
        return halls

    async def _sweep_hall(self, guild: discord.Guild, role: discord.Role, hall: discord.Thread) -> None:
        holders = {member.id for member in role.members if not member.bot}
        try:
            following = {thread_member.id for thread_member in await hall.fetch_members()}
        except discord.HTTPException:
            logger.warning("Couldn't list members of hall %s", hall.id, exc_info=True)
            return
        for member_id in missing_followers(holders, following):
            if (member := guild.get_member(member_id)) is not None:
                await self._set_following(hall, member, following=True)


async def setup(bot: SideQuestBot) -> None:
    """Register the cog."""
    await bot.add_cog(ClassHallsCog(bot))
