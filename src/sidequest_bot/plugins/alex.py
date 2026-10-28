"""A standing joke: when Alex pings the bot, it answers back, at most once every four hours.

The cooldown lives in memory, so a bot restart resets it.
"""

import logging
import time
from typing import TYPE_CHECKING, Final

import discord
from discord.ext import commands

from sidequest_bot.bot import SideQuestBot  # noqa: TC001  # discord.py evaluates app-command annotations at runtime

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

ALEX_USER_ID: Final = 235058370531426304
REPLY: Final = "fuck you alex"
COOLDOWN_SECONDS: Final = 4 * 60 * 60


class AlexCog(commands.Cog):
    """Replies to Alex's mentions of the bot, rate limited to once per ``COOLDOWN_SECONDS``."""

    def __init__(self, bot: SideQuestBot, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.bot = bot
        self._clock = clock
        self._last_reply: float | None = None

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Answer Alex when the bot is mentioned (a reply that pings the bot counts) and the cooldown has passed."""
        me = self.bot.user
        if me is None or message.author.id != ALEX_USER_ID:
            return
        if all(user.id != me.id for user in message.mentions):
            return
        now = self._clock()
        if self._last_reply is not None and now - self._last_reply < COOLDOWN_SECONDS:
            return
        previous, self._last_reply = self._last_reply, now
        try:
            await message.reply(REPLY, mention_author=False)
        except discord.HTTPException:
            # Didn't land, so don't burn the cooldown on it.
            self._last_reply = previous
            logger.warning("Couldn't reply to Alex in channel %s", message.channel.id, exc_info=True)


async def setup(bot: SideQuestBot) -> None:
    """Register the cog."""
    await bot.add_cog(AlexCog(bot))
