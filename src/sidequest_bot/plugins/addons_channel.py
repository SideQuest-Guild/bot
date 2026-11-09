"""Read-only ``#addons`` channel: the addons the guild requires and suggests, grouped by category.

Every ten minutes the bot fetches the catalog from the API and keeps ``#addons`` in the site guild (``WEB_GUILD_ID``)
in sync: creates the channel below ``#rules`` with the same visibility the first time, then edits, posts or deletes
its own messages so the channel always mirrors the current catalog without reposting unchanged content.
"""

import logging
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Final

import discord
from discord.ext import commands, tasks

from sidequest_bot.api_client import APIError, NotFoundError
from sidequest_bot.bot import SideQuestBot  # noqa: TC001  # discord.py evaluates app-command annotations at runtime
from sidequest_bot.lib.embeds import Colors, make_embed
from sidequest_common.schemas import ADDON_CATEGORY_LABELS, AddonCategory
from sidequest_common.settings import WebSettings

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sidequest_common.schemas import AddonSchema

logger = logging.getLogger(__name__)

CHECK_INTERVAL_MINUTES = 10
CHANNEL_NAME = "addons"
RULES_CHANNEL_NAME = "rules"
HISTORY_LIMIT = 50
"""How many of the channel's own recent messages are read back to sync against; the catalog is far smaller."""
DESCRIPTION_LIMIT: Final = 4096
"""Discord's hard cap on an embed's ``description``."""


@lru_cache(maxsize=1)
def _web_settings() -> WebSettings:
    """``WebSettings`` (``WEB_*`` env vars), shared with the API: ``GUILD_ID`` and ``BASE_URL`` for the addons link."""
    return WebSettings()


def find_text_channel(guild: discord.Guild, name: str) -> discord.TextChannel | None:
    """The one text channel called ``name`` anywhere in ``guild``, or ``None``."""
    for channel in guild.channels:
        if isinstance(channel, discord.TextChannel) and channel.name.casefold() == name.casefold():
            return channel
    return None


def _channel_topic(base_url: str) -> str:
    return f"Addons the guild recommends for WoW: Forever. Kept in step with {base_url}/addons/."


def build_intro(base_url: str) -> str:
    """Plain-words explanation of the channel, posted as the first message.

    The link sits in ``<>`` so Discord adds no preview embed, which would make the intro never match on the next sync.
    """
    return (
        "Addons for WoW: Forever, grouped by who they're for. Run the **Required for raids** ones on raid nights; "
        f"the rest are suggestions. The same list, with versions, is at <{base_url}/addons/>."
    )


def _addon_line(addon: AddonSchema) -> str:
    return f"**[{addon.name}]({addon.download_url})**: {addon.summary}"


def _pack_lines(lines: Sequence[str], limit: int = DESCRIPTION_LIMIT) -> list[str]:
    """Greedily pack ``lines`` into newline-joined chunks no longer than ``limit`` characters."""
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in lines:
        added = len(line) + (1 if current else 0)
        if current and current_len + added > limit:
            chunks.append("\n".join(current))
            current = [line]
            current_len = len(line)
        else:
            current.append(line)
            current_len += added
    if current:
        chunks.append("\n".join(current))
    return chunks or [""]


class RenderedPost:
    """One message to sync: either plain ``content`` (the intro) or a single category ``embed``, never both."""

    __slots__ = ("content", "embed")

    def __init__(self, *, content: str | None = None, embed: discord.Embed | None = None) -> None:
        self.content = content
        self.embed = embed


def render_posts(addons: Sequence[AddonSchema], base_url: str) -> list[RenderedPost]:
    """The intro message, then one embed per non-empty category (in enum order, split if it would overflow).

    Within a category, guild-made addons sort first, then alphabetically.
    """
    posts = [RenderedPost(content=build_intro(base_url))]
    for category in AddonCategory:
        group = [addon for addon in addons if addon.category == category]
        if not group:
            continue
        ordered = sorted(group, key=lambda addon: (addon.origin != "guild", addon.name.casefold()))
        lines = [_addon_line(addon) for addon in ordered]
        for chunk in _pack_lines(lines):
            embed = make_embed(ADDON_CATEGORY_LABELS[category], chunk, color=Colors.SITE_GOLD, timestamp=False)
            posts.append(RenderedPost(embed=embed))
    return posts


def _embed_key(embed: discord.Embed) -> tuple[str | None, str | None, int | None]:
    """What the bot sets on a category embed, compared field by field: Discord may add keys of its own to a sent one."""
    return embed.title, embed.description, embed.colour.value if embed.colour is not None else None


def _post_matches(message: discord.Message, post: RenderedPost) -> bool:
    """Whether ``message`` already shows ``post``'s content and embed, so editing it would be a no-op."""
    content_matches = (message.content or "") == (post.content or "")
    desired_embeds = [_embed_key(post.embed)] if post.embed is not None else []
    return content_matches and [_embed_key(embed) for embed in message.embeds] == desired_embeds


class AddonsChannelCog(commands.Cog):
    """Keeps the site guild's ``#addons`` channel in sync with the API's addon catalog."""

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
            addons = await self.bot.api.list_addons()
        except NotFoundError:
            # The addons endpoint may not exist yet on an old API pod mid-rollout; retry next tick.
            logger.info("Addons endpoint not available yet; retrying next tick")
            return
        except APIError:
            # Must not escape: an unhandled exception stops a tasks.loop for good. Retried next tick.
            logger.warning("Failed to fetch addons", exc_info=True)
            return
        if not addons:
            # Nothing to show yet; leave whatever is already in the channel alone.
            return
        try:
            await self._sync_guild(guild, addons)
        except discord.HTTPException:
            logger.warning("Failed to sync #%s in guild %s", CHANNEL_NAME, guild.id, exc_info=True)

    @_check_loop.before_loop
    async def _before_check_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def _sync_guild(self, guild: discord.Guild, addons: Sequence[AddonSchema]) -> None:
        channel = await self._ensure_channel(guild)
        if channel is None:
            return
        await self._sync_messages(channel, addons)

    async def _ensure_channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        """The existing ``#addons`` channel, or a new one placed right below ``#rules`` with its overwrites."""
        existing = find_text_channel(guild, CHANNEL_NAME)
        if existing is not None:
            return existing
        rules = find_text_channel(guild, RULES_CHANNEL_NAME)
        if rules is None:
            logger.warning("No #%s channel in guild %s; can't place #%s", RULES_CHANNEL_NAME, guild.id, CHANNEL_NAME)
            return None
        channel = await guild.create_text_channel(
            CHANNEL_NAME,
            overwrites=dict(rules.overwrites),
            topic=_channel_topic(_web_settings().BASE_URL),
            reason="Auto-created to list recommended addons, visible like #rules",
        )
        await channel.move(after=rules)
        return channel

    async def _sync_messages(self, channel: discord.TextChannel, addons: Sequence[AddonSchema]) -> None:
        """Edit, post or delete the channel's own messages so they match ``addons``, touching nothing else."""
        me = self.bot.user
        if me is None:
            return
        posts = render_posts(addons, _web_settings().BASE_URL)
        existing = [
            message
            async for message in channel.history(limit=HISTORY_LIMIT, oldest_first=True)
            if message.author.id == me.id
        ]
        for index, post in enumerate(posts):
            if index < len(existing):
                if not _post_matches(existing[index], post):
                    await existing[index].edit(
                        content=post.content, embed=post.embed, allowed_mentions=discord.AllowedMentions.none()
                    )
            else:
                send_kwargs: dict[str, Any] = {"allowed_mentions": discord.AllowedMentions.none()}
                if post.content is not None:
                    send_kwargs["content"] = post.content
                if post.embed is not None:
                    send_kwargs["embed"] = post.embed
                await channel.send(**send_kwargs)
        for surplus in existing[len(posts) :]:
            await surplus.delete()


async def setup(bot: SideQuestBot) -> None:
    """Register the cog."""
    await bot.add_cog(AddonsChannelCog(bot))
