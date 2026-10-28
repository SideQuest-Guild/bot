"""Alex pings the bot, the bot answers back, once every four hours."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from sidequest_bot.plugins.alex import ALEX_USER_ID, COOLDOWN_SECONDS, REPLY, AlexCog

BOT_ID = 4242


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


def _cog(clock: _Clock) -> AlexCog:
    bot = SimpleNamespace(user=SimpleNamespace(id=BOT_ID))
    return AlexCog(bot, clock=clock)  # type: ignore[arg-type]


def _message(*, author_id: int = ALEX_USER_ID, mentions: list[int] | None = None) -> MagicMock:
    message = MagicMock(spec=discord.Message)
    message.author = SimpleNamespace(id=author_id)
    message.channel = SimpleNamespace(id=5)
    message.mentions = [SimpleNamespace(id=user_id) for user_id in (mentions if mentions is not None else [BOT_ID])]
    message.reply = AsyncMock()
    return message


async def test_replies_when_alex_mentions_the_bot() -> None:
    message = _message()
    await _cog(_Clock()).on_message(message)
    message.reply.assert_awaited_once_with(REPLY, mention_author=False)


@pytest.mark.parametrize(
    ("author_id", "mentions"),
    [(ALEX_USER_ID + 1, [BOT_ID]), (ALEX_USER_ID, []), (ALEX_USER_ID, [BOT_ID + 1])],
    ids=["someone else", "no mention", "mentions another user"],
)
async def test_ignores_other_messages(author_id: int, mentions: list[int]) -> None:
    message = _message(author_id=author_id, mentions=mentions)
    await _cog(_Clock()).on_message(message)
    message.reply.assert_not_awaited()


async def test_cooldown_allows_one_reply_every_four_hours() -> None:
    clock = _Clock()
    cog = _cog(clock)
    first, too_soon, later = _message(), _message(), _message()

    await cog.on_message(first)
    clock.now += COOLDOWN_SECONDS - 1
    await cog.on_message(too_soon)
    clock.now += 1
    await cog.on_message(later)

    first.reply.assert_awaited_once()
    too_soon.reply.assert_not_awaited()
    later.reply.assert_awaited_once()


async def test_failed_reply_does_not_start_the_cooldown() -> None:
    cog = _cog(_Clock())
    failed = _message()
    failed.reply.side_effect = discord.HTTPException(MagicMock(status=500), "boom")
    retry = _message()

    await cog.on_message(failed)
    await cog.on_message(retry)

    retry.reply.assert_awaited_once()
