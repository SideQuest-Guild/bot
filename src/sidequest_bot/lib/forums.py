"""Finding the bot's own named posts in a forum channel, whether Discord has archived them or not."""

from typing import Final

import discord

CLASS_HALLS_FORUM: Final = "class-halls"
"""The forum holding each class's hall, the class picker post and the gear sims post."""


async def find_post(forum: discord.ForumChannel, name: str) -> discord.Thread | None:
    """The post called ``name`` in ``forum``: an active one first, else an archived one, else ``None``."""
    post = discord.utils.get(forum.threads, name=name)
    if post is not None:
        return post
    async for thread in forum.archived_threads(limit=None):
        if thread.name == name:
            return thread
    return None
