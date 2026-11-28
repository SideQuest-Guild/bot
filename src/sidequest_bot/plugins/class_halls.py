"""Class halls: a forum post per role that follows the role's members into it, with that class's guides and changes.

A thread named ``"<role name> Hall"`` in any forum channel is that role's hall (e.g. ``Warrior Hall`` for
``@Warrior``). Gaining the role adds the member to the hall's thread, so it shows up in their channel list; losing
the role takes them out again. Every hall stays open for anyone who can see the forum to browse and follow. No
configuration: renaming a role or its post is all it takes to pair or unpair them.

A sweep at startup and every few hours adds role holders the bot missed while offline. It never removes anyone:
people follow halls on their own (or by posting in them), and that's theirs to keep.

A class's hall also gets a pinned post of the guides the guild recommends (``lib/class_guides.py``), posted when the
hall appears and edited by the sweep when the list changes. The WoW: Forever feed posts each class's hotfixes into
its hall through :meth:`ClassHallsCog.find_hall`.

The sweep also keeps a pinned **Choose your class** post at the top of the class-halls forum, where the class role
menu lives (moved there with ``/rolemenu move``).
"""

import logging
from typing import Final

import discord
from discord.ext import commands, tasks

from sidequest_bot.bot import SideQuestBot  # noqa: TC001  # discord.py evaluates app-command annotations at runtime
from sidequest_bot.lib.class_guides import CLASS_GUIDES
from sidequest_bot.lib.embeds import link_button
from sidequest_bot.lib.forums import CLASS_HALLS_FORUM, find_post
from sidequest_bot.plugins.characters import class_color

logger = logging.getLogger(__name__)

HALL_SUFFIX: Final = " Hall"
SWEEP_INTERVAL_HOURS: Final = 6
GUIDES_HISTORY_LIMIT: Final = 100
"""Recent hall messages searched for the guides post when it isn't among the pins (pinning failed)."""
CHOOSER_POST_NAME: Final = "Choose your class"
CHOOSER_INTRO: Final = (
    "Click your class below to get its role. It also adds you to your class's hall in this forum, where the guild's "
    "recommended guides and Blizzard's hotfixes for your class are posted. Click it again to drop the role."
)


def hall_name(role_name: str) -> str:
    """The forum post name that marks ``role_name``'s hall."""
    return f"{role_name}{HALL_SUFFIX}"


def role_changes(before: set[int], after: set[int]) -> tuple[set[int], set[int]]:
    """Role ids gained and lost between two snapshots of a member's roles."""
    return after - before, before - after


def missing_followers(role_holders: set[int], hall_members: set[int]) -> set[int]:
    """Role holders who aren't following their hall yet."""
    return role_holders - hall_members


def guides_title(wow_class: str) -> str:
    """The guides post's embed title, which is also how the bot finds its own post again."""
    return f"Recommended {wow_class} guides"


def guides_embed(wow_class: str) -> discord.Embed:
    """The guides post: what the hall's buttons are, in the class's color."""
    return discord.Embed(
        title=guides_title(wow_class),
        description=(
            f"Guides the guild recommends for {wow_class}s, from Method and Mobalytics. "
            f"Blizzard's {wow_class} hotfixes are posted in this hall as they're announced."
        ),
        colour=class_color(wow_class.lower()),
    )


def guides_view(wow_class: str) -> discord.ui.View:
    """One row of link buttons per guide site, in ``CLASS_GUIDES`` order."""
    view = discord.ui.View(timeout=None)
    sites: list[str] = []
    for guide in CLASS_GUIDES[wow_class]:
        if guide.site not in sites:
            sites.append(guide.site)
        button = link_button(f"{guide.site} · {guide.label}", guide.url)
        button.row = sites.index(guide.site)
        view.add_item(button)
    return view


def view_buttons(view: discord.ui.View) -> list[tuple[str | None, str | None]]:
    """``(label, url)`` of each button in a view about to be sent, row by row."""
    return [(item.label, item.url) for item in view.children if isinstance(item, discord.ui.Button)]


def message_buttons(message: discord.Message) -> list[tuple[str | None, str | None]]:
    """``(label, url)`` of each button on a sent message, row by row."""
    return [
        (child.label, child.url)
        for row in message.components
        if isinstance(row, discord.ActionRow)
        for child in row.children
        if isinstance(child, discord.Button)
    ]


def is_guides_post(message: discord.Message, bot_id: int, wow_class: str) -> bool:
    """Whether ``message`` is the bot's guides post for ``wow_class``."""
    return message.author.id == bot_id and bool(message.embeds) and message.embeds[0].title == guides_title(wow_class)


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

    def has_halls(self, guild_id: int) -> bool:
        """Whether the last sweep (or a new post since) found any hall in ``guild_id``."""
        return any(key_guild_id == guild_id for key_guild_id, _ in self._hall_ids)

    async def find_hall(self, guild: discord.Guild, role_name: str) -> discord.Thread | None:
        """``role_name``'s hall from the index the sweep builds (no forum scan on every role change)."""
        name = hall_name(role_name)
        thread_id = self._hall_ids.get((guild.id, name))
        if thread_id is None:
            return None
        if (thread := guild.get_thread(thread_id)) is not None:
            return thread
        try:
            channel = await guild.fetch_channel(thread_id)
        except discord.NotFound:
            self._hall_ids.pop((guild.id, name), None)
            return None
        except discord.HTTPException:
            logger.warning("Couldn't fetch hall %s", thread_id, exc_info=True)
            return None
        return channel if isinstance(channel, discord.Thread) else None

    @commands.Cog.listener()
    async def on_thread_create(self, thread: discord.Thread) -> None:
        """Index a new forum post named like a hall right away, and give a class's hall its guides post."""
        if not (thread.name.endswith(HALL_SUFFIX) and isinstance(thread.parent, discord.ForumChannel)):
            return
        self._hall_ids[(thread.guild.id, thread.name)] = thread.id
        wow_class = thread.name.removesuffix(HALL_SUFFIX)
        if wow_class in CLASS_GUIDES:
            await self._sync_guides(thread, wow_class)

    @staticmethod
    async def reopen(thread: discord.Thread) -> bool:
        """Unarchive ``thread`` if needed (archived threads take no members or posts). False if that failed."""
        if not thread.archived:
            return True
        try:
            await thread.edit(archived=False)
        except discord.HTTPException:
            logger.warning("Couldn't unarchive hall %s", thread.id, exc_info=True)
            return False
        return True

    # --- Guides post -----------------------------------------------------------------------------------------

    async def _find_guides(self, hall: discord.Thread, bot_id: int, wow_class: str) -> discord.Message | None:
        """The bot's guides post: among the pins, or else in recent history (if pinning it failed)."""
        async for message in hall.pins():
            if is_guides_post(message, bot_id, wow_class):
                return message
        async for message in hall.history(limit=GUIDES_HISTORY_LIMIT):
            if is_guides_post(message, bot_id, wow_class):
                return message
        return None

    async def _sync_guides(self, hall: discord.Thread, wow_class: str) -> None:
        """Post and pin ``wow_class``'s guides in ``hall``, or edit the post if the list changed since."""
        me = self.bot.user
        if me is None:
            return
        embed, view = guides_embed(wow_class), guides_view(wow_class)
        try:
            existing = await self._find_guides(hall, me.id, wow_class)
            wanted = (embed.title, embed.description, view_buttons(view))
            if existing is not None and (
                (existing.embeds[0].title, existing.embeds[0].description, message_buttons(existing)) == wanted
            ):
                return
            if not await self.reopen(hall):
                return
            if existing is not None:
                await existing.edit(embed=embed, view=view)
                return
            message = await hall.send(embed=embed, view=view)
            await message.pin(reason=f"{wow_class} guides")
        except discord.HTTPException:
            logger.warning("Couldn't post the guides in hall %s", hall.id, exc_info=True)

    # --- Role changes ----------------------------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        """Follow ``after`` into the halls of roles they gained, and out of the ones they lost."""
        if after.bot:
            return
        gained, lost = role_changes({role.id for role in before.roles}, {role.id for role in after.roles})
        for role_id in gained | lost:
            role = after.guild.get_role(role_id)
            if role is None or (hall := await self.find_hall(after.guild, role.name)) is None:
                continue
            await self._set_following(hall, after, following=role_id in gained)

    async def _set_following(self, hall: discord.Thread, member: discord.Member, *, following: bool) -> None:
        try:
            if following:
                if await self.reopen(hall):
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
            for name, hall in halls.items():
                if (wow_class := name.removesuffix(HALL_SUFFIX)) in CLASS_GUIDES:
                    await self._sync_guides(hall, wow_class)
            await self._sync_chooser(guild)

    async def _sync_chooser(self, guild: discord.Guild) -> None:
        """Create the class picker post in the class-halls forum if it's missing, reopen it, and keep it pinned.

        A forum has one pinned post, so another pinned post is unpinned first. The class role menu itself goes in
        with ``/rolemenu move``; this only keeps the post.
        """
        forum = discord.utils.get(guild.forums, name=CLASS_HALLS_FORUM)
        if forum is None:
            return
        try:
            post = await find_post(forum, CHOOSER_POST_NAME)
            if post is None:
                post = (await forum.create_thread(name=CHOOSER_POST_NAME, content=CHOOSER_INTRO)).thread
            elif not await self.reopen(post):
                return
            if post.flags.pinned:
                return
            for other in forum.threads:
                if other.id != post.id and other.flags.pinned:
                    await other.edit(pinned=False)
            await post.edit(pinned=True)
        except discord.HTTPException:
            logger.warning("Couldn't keep the %s post in guild %s", CHOOSER_POST_NAME, guild.id, exc_info=True)

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
