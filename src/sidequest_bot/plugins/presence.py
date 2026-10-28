"""The bot's status line: a launch countdown, the next guild event, and some Azeroth flavour, rotated.

Bots can't show true Rich Presence (art, buttons, timers come from a game client), only an activity line: Playing,
Listening to, Watching, Competing in, or a custom status. While a guild event is running the bot pins
"Competing in <event>" instead of rotating.
"""

import logging
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Final
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks

from sidequest_bot.api_client import APIError
from sidequest_bot.bot import SideQuestBot  # noqa: TC001  # discord.py evaluates app-command annotations at runtime

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sidequest_common.schemas import EventSummary

logger = logging.getLogger(__name__)

ROTATE_SECONDS: Final = 180
"""Well under Discord's presence-update rate limit."""

LAUNCH_DATE: Final = date(2026, 11, 4)
"""World of Warcraft: Forever launch (same date as the website's ``LAUNCH``)."""

GUILD_TZ: Final = ZoneInfo("America/Chicago")
"""US guild: "days until launch" rolls over at midnight Central."""

DEFAULT_EVENT_LENGTH: Final = timedelta(hours=3)
"""How long an event without an end time counts as running."""

FLAVOUR: Final[tuple[discord.BaseActivity, ...]] = (
    discord.Activity(type=discord.ActivityType.listening, name="the Goldshire Inn bard"),
    discord.Activity(type=discord.ActivityType.watching, name="for murlocs at the docks"),
    discord.Activity(type=discord.ActivityType.competing, name="a Stranglethorn fishing contest"),
    discord.CustomActivity(name="Recruiting: every class, every role"),
)


def launch_activity(now: datetime) -> discord.BaseActivity:
    """Countdown before launch, a launch-day line on the day, then simply playing the game."""
    days = (LAUNCH_DATE - now.astimezone(GUILD_TZ).date()).days
    if days > 1:
        return discord.CustomActivity(name=f"{days} days until Azeroth opens")
    if days == 1:
        return discord.CustomActivity(name="Azeroth opens tomorrow")
    if days == 0:
        return discord.CustomActivity(name="Launch day! See you in Azeroth")
    return discord.Game(name="World of Warcraft: Forever")


def _until(delta: timedelta) -> str:
    hours = int(delta.total_seconds() // 3600)
    if hours < 1:
        return "starting soon"
    if hours < 24:  # noqa: PLR2004
        return f"in {hours}h"
    days = delta.days
    return "in a day" if days == 1 else f"in {days} days"


def build_rotation(now: datetime, events: Sequence[EventSummary]) -> list[discord.BaseActivity]:
    """Activities to cycle through. A running event pins a single "Competing in" line."""
    scheduled = sorted((e for e in events if e.status == "scheduled"), key=lambda e: e.starts_at)
    for event in scheduled:
        if event.starts_at <= now < (event.ends_at or event.starts_at + DEFAULT_EVENT_LENGTH):
            return [discord.Activity(type=discord.ActivityType.competing, name=event.title)]
    rotation = [launch_activity(now)]
    upcoming = next((e for e in scheduled if e.starts_at > now), None)
    if upcoming is not None:
        rotation.append(discord.CustomActivity(name=f"Next up: {upcoming.title}, {_until(upcoming.starts_at - now)}"))
    rotation.extend(FLAVOUR)
    return rotation


def _key(activity: discord.BaseActivity) -> tuple[object, str | None]:
    return getattr(activity, "type", None), getattr(activity, "name", None)


class PresenceCog(commands.Cog):
    """Rotates the bot's activity every ``ROTATE_SECONDS``."""

    def __init__(self, bot: SideQuestBot) -> None:
        self.bot = bot
        self._tick = 0
        self._current: tuple[object, str | None] | None = None
        self._rotate.start()

    async def cog_unload(self) -> None:
        """Stop rotating when the cog is unloaded."""
        self._rotate.cancel()

    @tasks.loop(seconds=ROTATE_SECONDS)
    async def _rotate(self) -> None:
        try:
            events = await self.bot.api.list_upcoming_events()
        except APIError:
            logger.debug("Couldn't load events for the status line", exc_info=True)
            events = []
        rotation = build_rotation(datetime.now(UTC), events)
        activity = rotation[self._tick % len(rotation)]
        self._tick += 1
        if _key(activity) == self._current:
            return
        try:
            await self.bot.change_presence(activity=activity)
        except discord.HTTPException:
            logger.warning("Couldn't update the bot's status", exc_info=True)
            return
        self._current = _key(activity)

    @_rotate.before_loop
    async def _before_rotate(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: SideQuestBot) -> None:
    """Register the cog."""
    await bot.add_cog(PresenceCog(bot))
