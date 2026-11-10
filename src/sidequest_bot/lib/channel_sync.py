"""Shared logic for read-only channels the bot keeps mirrored to backend data (``#addons``, ``#roles``, ...).

Each such channel is created once, below a fixed anchor channel with the anchor's own visibility, then holds one
message per rendered section: plain ``content`` for an intro, or a single embed per section. :func:`sync_messages`
edits, posts or deletes the channel's own messages so they match the wanted list, touching nothing else and never
reposting unchanged content.
"""

from typing import TYPE_CHECKING, Any

import discord

if TYPE_CHECKING:
    from collections.abc import Sequence


def find_text_channel(guild: discord.Guild, name: str) -> discord.TextChannel | None:
    """The one text channel called ``name`` anywhere in ``guild``, or ``None``."""
    for channel in guild.channels:
        if isinstance(channel, discord.TextChannel) and channel.name.casefold() == name.casefold():
            return channel
    return None


async def ensure_channel_below(
    guild: discord.Guild, *, name: str, anchor_name: str, topic: str, reason: str
) -> discord.TextChannel | None:
    """The existing channel called ``name``, or a new one placed right below ``anchor_name`` with its overwrites.

    ``None`` if neither ``name`` nor ``anchor_name`` exists yet.
    """
    existing = find_text_channel(guild, name)
    if existing is not None:
        return existing
    anchor = find_text_channel(guild, anchor_name)
    if anchor is None:
        return None
    channel = await guild.create_text_channel(name, overwrites=dict(anchor.overwrites), topic=topic, reason=reason)
    await channel.move(after=anchor)
    return channel


class RenderedPost:
    """One message to sync: either plain ``content`` (an intro) or a single section ``embed``, never both."""

    __slots__ = ("content", "embed")

    def __init__(self, *, content: str | None = None, embed: discord.Embed | None = None) -> None:
        self.content = content
        self.embed = embed


def _embed_key(embed: discord.Embed) -> tuple[str | None, str | None, int | None]:
    """What a plugin sets on an embed, compared field by field: Discord may add keys of its own to a sent one."""
    return embed.title, embed.description, embed.colour.value if embed.colour is not None else None


def _post_matches(message: discord.Message, post: RenderedPost) -> bool:
    """Whether ``message`` already shows ``post``'s content and embed, so editing it would be a no-op."""
    content_matches = (message.content or "") == (post.content or "")
    desired_embeds = [_embed_key(post.embed)] if post.embed is not None else []
    return content_matches and [_embed_key(embed) for embed in message.embeds] == desired_embeds


async def sync_messages(
    channel: discord.TextChannel, bot_user_id: int, posts: Sequence[RenderedPost], *, history_limit: int = 50
) -> None:
    """Edit, post or delete ``channel``'s own messages so they match ``posts``, touching nothing else."""
    existing = [
        message
        async for message in channel.history(limit=history_limit, oldest_first=True)
        if message.author.id == bot_user_id
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
