"""Read-only ``#roles`` channel: every Discord role the server uses and how it's earned.

Every ten minutes the bot keeps ``#roles`` in the site guild (``WEB_GUILD_ID``) in sync: creates the channel below
``#addons`` with the same visibility the first time, then edits, posts or deletes its own messages so the channel
mirrors the current guild config, level rewards and :data:`BADGE_LIST` without reposting unchanged content. Each
role is shown as a mention, resolved by its configured id where the guild config tracks one, else by its exact
Discord name; a role that doesn't currently exist still gets its own line, just without a working mention.
"""

import logging
import re
from functools import lru_cache
from typing import TYPE_CHECKING

import discord
from discord.ext import commands, tasks

from sidequest_bot.api_client import APIError
from sidequest_bot.bot import SideQuestBot  # noqa: TC001  # discord.py evaluates app-command annotations at runtime
from sidequest_bot.lib.channel_sync import RenderedPost, ensure_channel_below, sync_messages
from sidequest_bot.lib.embeds import Colors, make_embed, pack_lines
from sidequest_bot.plugins.addons_channel import CHANNEL_NAME as ADDONS_CHANNEL_NAME
from sidequest_common.schemas import BADGE_LIST
from sidequest_common.settings import WebSettings

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from sidequest_common.schemas import GuildConfigSchema, LevelRoleSchema

logger = logging.getLogger(__name__)

CHECK_INTERVAL_MINUTES = 10
CHANNEL_NAME = "roles"
HISTORY_LIMIT = 50
"""How many of the channel's own recent messages are read back to sync against; the catalog is far smaller."""

_CHANNEL_TOPIC = "Every role in the server and how it's earned. Kept up to date by the bot."

_INTRO = (
    "Every role in the server, and how it's earned. Officers give the guild ranks (except New Arrival) and the "
    "hand-given roles; the bot and the site grant and remove everything else on their own."
)

_RANK_ENTRIES: tuple[tuple[str, str], ...] = (
    ("Quest Master", "A guild leader."),
    ("Quest Guide", "Picked by the Quest Masters; an officer."),
    ("Expedition Leader", "Picked by the Quest Masters and Quest Guides; leads raids."),
    ("Seasoned Adventurer", "Character level 60, and the Raid Regular badge (10 raids with the guild)."),
    ("Adventurer", "14 days in the guild, and the First Steps badge (came to a guild event)."),
    (
        "New Arrival",
        (
            "Confirmed in the guild: linking Battle.net with a character on the guild roster, or a Quest Master "
            "approving or confirming you."
        ),
    ),
    ("Alternate Path", "Given by hand for an alt character."),
)

_HAND_GIVEN_ENTRIES: tuple[tuple[str, str], ...] = (
    ("NPC", "Friends who don't play the game but want to be in the Discord."),
    ("Familiar", "Automated or bot accounts."),
)

_AUTOMATIC_ROLE_ENTRIES: tuple[tuple[str, str], ...] = (
    ("At the Gate", "In the server but hasn't signed in or been confirmed by a Quest Master yet."),
    ("Signed the Ledger", "Signed in with Discord on the site."),
    ("Battle-Tagged", "Linked Battle.net on the account page."),
    ("Away on a Quest", "Marked away dates on the account page that include today."),
    ("PUG", "Invited to a PUG group from #lfg; removed 30 minutes after it ends unless they sign the ledger."),
)

_CLASS_ROLES_LINE = (
    "Class roles: pick yours in `#choose-your-class`, which also adds you to that class's post in `#class-halls`."
)

_BADGES_NOTE = (
    "A badge, once earned, is never taken away, except **Top Helper**, which moves to the next month's winner."
)

_CONFIGURED_ROLE_ATTRS: dict[str, str] = {
    "New Arrival": "roster_role_id",
    "At the Gate": "gate_role_id",
    "Signed the Ledger": "linked_role_id",
    "Battle-Tagged": "battlenet_role_id",
    "Away on a Quest": "away_role_id",
    "PUG": "pug_role_id",
}
"""Role names whose id the guild config tracks directly, mapped to the config's attribute name."""


@lru_cache(maxsize=1)
def _web_settings() -> WebSettings:
    """``WebSettings`` (``WEB_*`` env vars), shared with the API: ``GUILD_ID`` for the site guild."""
    return WebSettings()


def _emoji_key(name: str) -> str:
    """A role's display name, normalized to match its icon file's emoji name (``Quest Master`` -> ``quest_master``)."""
    return re.sub(r"[^a-z0-9]+", "_", name.casefold()).strip("_")


def find_role(guild: discord.Guild, name: str) -> discord.Role | None:
    """The one role called ``name`` anywhere in ``guild`` (case-insensitive), or ``None``."""
    for role in guild.roles:
        if role.name.casefold() == name.casefold():
            return role
    return None


def resolve_role(guild: discord.Guild, name: str, config: GuildConfigSchema) -> discord.Role | None:
    """``name``'s role: by its configured id if the guild config tracks one for it, else by its exact Discord name."""
    attr = _CONFIGURED_ROLE_ATTRS.get(name)
    role_id = getattr(config, attr) if attr else None
    if role_id is not None:
        role = guild.get_role(role_id)
        if role is not None:
            return role
    return find_role(guild, name)


def _role_line(emoji_by_name: Mapping[str, str], role: discord.Role | None, name: str, how: str) -> str:
    """``emoji mention: how``, falling back to the bare name in bold if ``role`` no longer exists."""
    emoji = emoji_by_name.get(_emoji_key(role.name if role is not None else name), "")
    prefix = f"{emoji} " if emoji else ""
    mention = role.mention if role is not None else f"**{name}**"
    return f"{prefix}{mention}: {how}"


def _entry_lines(
    guild: discord.Guild,
    config: GuildConfigSchema,
    emoji_by_name: Mapping[str, str],
    entries: Sequence[tuple[str, str]],
) -> list[str]:
    return [_role_line(emoji_by_name, resolve_role(guild, name, config), name, how) for name, how in entries]


def _level_role_lines(
    guild: discord.Guild, level_roles: Sequence[LevelRoleSchema], emoji_by_name: Mapping[str, str]
) -> list[str]:
    lines = []
    for reward in level_roles:
        role = guild.get_role(reward.role_id)
        if role is None:
            continue
        lines.append(_role_line(emoji_by_name, role, role.name, f"Reached level {reward.level}."))
    return lines


def _badge_lines(guild: discord.Guild, config: GuildConfigSchema, emoji_by_name: Mapping[str, str]) -> list[str]:
    lines = [_BADGES_NOTE]
    for badge in BADGE_LIST:
        role_id = config.badge_role_ids.get(badge.id)
        role = guild.get_role(role_id) if role_id is not None else None
        if role is None:
            role = find_role(guild, badge.name)
        lines.append(_role_line(emoji_by_name, role, badge.name, badge.description))
    return lines


def render_posts(
    guild: discord.Guild, config: GuildConfigSchema, level_roles: Sequence[LevelRoleSchema]
) -> list[RenderedPost]:
    """The intro message, then one embed per section (split if it would overflow the embed description limit)."""
    emoji_by_name = {emoji.name.lower(): str(emoji) for emoji in guild.emojis}
    posts = [RenderedPost(content=_INTRO)]

    def section(title: str, lines: Sequence[str]) -> None:
        for chunk in pack_lines(lines):
            embed = make_embed(title, chunk, color=Colors.SITE_GOLD, timestamp=False)
            posts.append(RenderedPost(embed=embed))

    section("Guild ranks", _entry_lines(guild, config, emoji_by_name, _RANK_ENTRIES))
    section("Given by hand", _entry_lines(guild, config, emoji_by_name, _HAND_GIVEN_ENTRIES))
    automatic_lines = [
        *_entry_lines(guild, config, emoji_by_name, _AUTOMATIC_ROLE_ENTRIES),
        *_level_role_lines(guild, level_roles, emoji_by_name),
        _CLASS_ROLES_LINE,
    ]
    section("Automatic roles", automatic_lines)
    section("Hall of Heroes badges", _badge_lines(guild, config, emoji_by_name))
    return posts


class RolesChannelCog(commands.Cog):
    """Keeps the site guild's ``#roles`` channel in sync with the guild config, level rewards and badge list."""

    def __init__(self, bot: SideQuestBot) -> None:
        self.bot = bot
        self._check_loop.start()

    async def cog_unload(self) -> None:
        """Stop the check loop when the cog is unloaded."""
        self._check_loop.cancel()

    @tasks.loop(minutes=CHECK_INTERVAL_MINUTES)
    async def _check_loop(self) -> None:
        guild = self.bot.get_guild(_web_settings().GUILD_ID)
        if guild is None:
            return
        try:
            config = await self.bot.configs.get(guild.id)
            level_roles = await self.bot.api.list_level_roles(guild.id)
        except APIError:
            # Must not escape: an unhandled exception stops a tasks.loop for good. Retried next tick.
            logger.warning("Failed to fetch guild config or level roles", exc_info=True)
            return
        try:
            await self._sync_guild(guild, config, level_roles)
        except discord.HTTPException:
            logger.warning("Failed to sync #%s in guild %s", CHANNEL_NAME, guild.id, exc_info=True)

    @_check_loop.before_loop
    async def _before_check_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def _sync_guild(
        self, guild: discord.Guild, config: GuildConfigSchema, level_roles: Sequence[LevelRoleSchema]
    ) -> None:
        channel = await self._ensure_channel(guild)
        if channel is None:
            return
        await self._sync_messages(channel, config, level_roles)

    async def _ensure_channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        """The existing ``#roles`` channel, or a new one placed right below ``#addons`` with its overwrites."""
        channel = await ensure_channel_below(
            guild,
            name=CHANNEL_NAME,
            anchor_name=ADDONS_CHANNEL_NAME,
            topic=_CHANNEL_TOPIC,
            reason="Auto-created to list every role and how it's earned, visible like #addons",
        )
        if channel is None:
            logger.warning("No #%s channel in guild %s; can't place #%s", ADDONS_CHANNEL_NAME, guild.id, CHANNEL_NAME)
        return channel

    async def _sync_messages(
        self, channel: discord.TextChannel, config: GuildConfigSchema, level_roles: Sequence[LevelRoleSchema]
    ) -> None:
        """Edit, post or delete the channel's own messages so they match the current roles, touching nothing else."""
        me = self.bot.user
        if me is None:
            return
        posts = render_posts(channel.guild, config, level_roles)
        await sync_messages(channel, me.id, posts, history_limit=HISTORY_LIMIT)


async def setup(bot: SideQuestBot) -> None:
    """Register the cog."""
    await bot.add_cog(RolesChannelCog(bot))
