from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import discord

from sidequest_bot.plugins.stats_backfill import (
    BACKFILL_CHUNK_SIZE,
    chunk_buckets,
    collect_message_counts,
    is_backfill_eligible,
)
from sidequest_common.schemas import GuildConfigSchema

_GUILD_SENTINEL = object()


def _make_config(**overrides: Any) -> GuildConfigSchema:
    base: dict[str, Any] = {
        "guild_id": 1,
        "log_channel_id": None,
        "welcome_channel_id": None,
        "welcome_message": "hi",
        "auto_role_id": None,
        "linked_role_id": None,
        "battlenet_role_id": None,
        "leveling_enabled": True,
        "level_up_channel_id": None,
        "level_up_message": "GG {mention}, level {level}!",
        "xp_min": 15,
        "xp_max": 25,
        "xp_cooldown_seconds": 60,
        "level_roles_stack": True,
        "no_xp_channel_ids": [],
        "no_xp_role_ids": [],
        "automod_enabled": True,
        "automod_block_invites": True,
        "automod_block_links": False,
        "automod_max_mentions": 5,
        "automod_spam_messages": 5,
        "automod_spam_seconds": 5,
        "automod_timeout_seconds": 300,
        "automod_exempt_role_ids": [],
        "automod_exempt_channel_ids": [],
    }
    base.update(overrides)
    return GuildConfigSchema(**base)


def _make_message(
    *,
    author_bot: bool = False,
    webhook_id: int | None = None,
    system: bool = False,
    channel_id: int = 10,
    parent_id: int | None = None,
    author_role_ids: tuple[int, ...] = (),
    is_member: bool = True,
    created_at: datetime = datetime(2026, 1, 1, 12, 30, tzinfo=UTC),
) -> SimpleNamespace:
    roles = [SimpleNamespace(id=rid) for rid in author_role_ids]
    author: Any = SimpleNamespace(id=99, bot=author_bot, roles=roles) if is_member else SimpleNamespace(id=99)
    if not is_member:
        author.bot = author_bot
    channel: Any = SimpleNamespace(id=channel_id)
    if parent_id is not None:
        channel.parent_id = parent_id
    return SimpleNamespace(
        author=author,
        webhook_id=webhook_id,
        channel=channel,
        is_system=lambda: system,
        created_at=created_at,
    )


def _as_member(message: SimpleNamespace) -> SimpleNamespace:
    """Make ``message.author`` pass ``isinstance(..., discord.Member)`` by giving it that spec."""
    import unittest.mock as mock

    member = mock.MagicMock(spec=discord.Member)
    member.id = message.author.id
    member.bot = message.author.bot
    member.roles = message.author.roles
    message.author = member
    return message


# --- is_backfill_eligible


def test_bot_message_not_eligible() -> None:
    assert is_backfill_eligible(_make_message(author_bot=True), _make_config()) is False


def test_webhook_message_not_eligible() -> None:
    assert is_backfill_eligible(_make_message(webhook_id=555), _make_config()) is False


def test_system_message_not_eligible() -> None:
    assert is_backfill_eligible(_make_message(system=True), _make_config()) is False


def test_no_xp_channel_not_eligible() -> None:
    message = _make_message(channel_id=42)
    assert is_backfill_eligible(message, _make_config(no_xp_channel_ids=[42])) is False


def test_no_xp_thread_parent_channel_not_eligible() -> None:
    message = _make_message(channel_id=99, parent_id=42)
    assert is_backfill_eligible(message, _make_config(no_xp_channel_ids=[42])) is False


def test_no_xp_role_not_eligible_for_current_member() -> None:
    message = _as_member(_make_message(author_role_ids=(7, 8)))
    assert is_backfill_eligible(message, _make_config(no_xp_role_ids=[8])) is False


def test_no_xp_role_skipped_when_author_no_longer_a_member() -> None:
    """A departed author (e.g. a `discord.User`, not a `discord.Member`) can't be checked for no-XP roles."""
    message = _make_message(author_role_ids=(8,), is_member=False)
    assert is_backfill_eligible(message, _make_config(no_xp_role_ids=[8])) is True


def test_normal_message_eligible_even_with_leveling_disabled() -> None:
    message = _as_member(_make_message(channel_id=1, author_role_ids=(1, 2)))
    config = _make_config(leveling_enabled=False, no_xp_channel_ids=[99], no_xp_role_ids=[5])
    assert is_backfill_eligible(message, config) is True


# --- chunk_buckets


def test_chunk_buckets_splits_at_chunk_size() -> None:
    hour = datetime(2026, 1, 1, tzinfo=UTC)
    counts = {(user_id, hour): 1 for user_id in range(BACKFILL_CHUNK_SIZE + 1)}

    chunks = chunk_buckets(counts)

    assert [len(chunk) for chunk in chunks] == [BACKFILL_CHUNK_SIZE, 1]


def test_chunk_buckets_empty_counts_is_no_chunks() -> None:
    assert chunk_buckets({}) == []


# --- collect_message_counts


class _FakeChannel:
    def __init__(self, channel_id: int, messages: list[SimpleNamespace], *, forbidden: bool = False) -> None:
        self.id = channel_id
        self._messages = messages
        self._forbidden = forbidden
        self.threads: list[Any] = []

    async def history(self, *, after: datetime, limit: int | None = None, oldest_first: bool | None = None):
        if self._forbidden:
            raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Missing Access")
        for message in self._messages:
            if message.created_at > after:
                yield message

    async def archived_threads(self, *, private: bool = False, limit: int | None = None):
        return
        yield  # pragma: no cover  # makes this an async generator


def _fake_guild(*text_channels: _FakeChannel) -> SimpleNamespace:
    return SimpleNamespace(text_channels=list(text_channels), voice_channels=[], stage_channels=[], forums=[])


async def test_collect_message_counts_truncates_to_the_hour_and_counts_eligible_messages() -> None:
    channel = _FakeChannel(
        1,
        [
            _as_member(_make_message(channel_id=1, created_at=datetime(2026, 1, 1, 12, 5, tzinfo=UTC))),
            _as_member(_make_message(channel_id=1, created_at=datetime(2026, 1, 1, 12, 55, tzinfo=UTC))),
            _as_member(_make_message(channel_id=1, created_at=datetime(2026, 1, 1, 13, 1, tzinfo=UTC))),
            _as_member(
                _make_message(channel_id=1, author_bot=True, created_at=datetime(2026, 1, 1, 12, 10, tzinfo=UTC))
            ),
        ],
    )
    guild = _fake_guild(channel)

    result = await collect_message_counts(guild, _make_config(), since=datetime(2026, 1, 1, tzinfo=UTC))

    assert result.channels_scanned == 1
    assert result.messages_counted == 3
    assert result.counts[99, datetime(2026, 1, 1, 12, tzinfo=UTC)] == 2
    assert result.counts[99, datetime(2026, 1, 1, 13, tzinfo=UTC)] == 1


async def test_collect_message_counts_skips_messages_before_since() -> None:
    channel = _FakeChannel(
        1, [_as_member(_make_message(channel_id=1, created_at=datetime(2026, 1, 1, 0, 0, tzinfo=UTC)))]
    )
    guild = _fake_guild(channel)

    result = await collect_message_counts(guild, _make_config(), since=datetime(2026, 1, 2, tzinfo=UTC))

    assert result.messages_counted == 0
    assert result.counts == {}


async def test_collect_message_counts_skips_a_forbidden_channel_without_failing() -> None:
    forbidden = _FakeChannel(1, [], forbidden=True)
    readable = _FakeChannel(
        2, [_as_member(_make_message(channel_id=2, created_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC)))]
    )
    guild = _fake_guild(forbidden, readable)

    result = await collect_message_counts(guild, _make_config(), since=datetime(2026, 1, 1, tzinfo=UTC))

    assert result.channels_scanned == 2
    assert result.messages_counted == 1
