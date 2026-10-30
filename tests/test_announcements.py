"""Unit tests for the announcements plugin: posting pending announcements and marking them posted."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import discord

from sidequest_bot.api_client import APIError, NotFoundError
from sidequest_bot.plugins.announcements import AnnouncementsCog, build_announcement_embed
from sidequest_common.schemas import PendingAnnouncement


def _announcement(**overrides: object) -> PendingAnnouncement:
    base: dict[str, object] = {
        "id": uuid4(),
        "text": "Flasks for Friday please",
        "author": "Gandalf-Area52",
        "at": datetime(2026, 10, 30, 12, 0, tzinfo=UTC),
    }
    base.update(overrides)
    return PendingAnnouncement.model_validate(base)


def _make_cog(*, api: object, guilds: list[object] | None = None, channel_id: int | None = 777) -> AnnouncementsCog:
    cog = AnnouncementsCog.__new__(AnnouncementsCog)
    configs = AsyncMock()
    configs.get.return_value = SimpleNamespace(announcement_channel_id=channel_id)
    cog.bot = SimpleNamespace(api=api, guilds=guilds or [], configs=configs)
    return cog


def _fake_guild(*, guild_id: int = 42, channel: object | None = None) -> MagicMock:
    guild = MagicMock(spec=discord.Guild)
    guild.id = guild_id
    guild.get_channel.return_value = channel
    return guild


def _fake_channel(*, message_id: int = 999) -> MagicMock:
    channel = MagicMock(spec=discord.TextChannel)
    channel.send = AsyncMock(return_value=SimpleNamespace(id=message_id))
    return channel


class TestBuildAnnouncementEmbed:
    def test_embed_has_title_author_and_text(self) -> None:
        announcement = _announcement(text="Raid moved to 8pm", author="Thrall-Area52")
        embed = build_announcement_embed(announcement)

        assert embed.title == "Announcement"
        assert embed.description == "Raid moved to 8pm"
        assert embed.author.name == "Thrall-Area52"


class TestCheckGuild:
    async def test_posts_pending_announcements_and_marks_them_posted(self) -> None:
        channel = _fake_channel(message_id=1234)
        guild = _fake_guild(channel=channel)
        api = AsyncMock()
        announcement = _announcement()
        api.list_pending_announcements.return_value = [announcement]
        cog = _make_cog(api=api)

        await cog._check_guild(guild)

        channel.send.assert_awaited_once()
        api.mark_announcement_posted.assert_awaited_once_with(guild.id, announcement.id, 1234)

    async def test_no_channel_configured_skips_the_guild_without_polling(self) -> None:
        guild = _fake_guild()
        api = AsyncMock()
        cog = _make_cog(api=api, channel_id=None)

        await cog._check_guild(guild)

        api.list_pending_announcements.assert_not_awaited()
        api.mark_announcement_posted.assert_not_awaited()

    async def test_channel_id_set_but_not_resolvable_skips_without_polling(self) -> None:
        guild = _fake_guild(channel=None)
        api = AsyncMock()
        cog = _make_cog(api=api)

        await cog._check_guild(guild)

        api.list_pending_announcements.assert_not_awaited()


class TestPostOne:
    async def test_api_error_marking_posted_leaves_it_unposted_for_next_poll(self) -> None:
        """A failed ack must not raise: the announcement stays pending and is posted again (and re-acked) next tick."""
        channel = _fake_channel()
        guild = _fake_guild(channel=channel)
        api = AsyncMock()
        api.mark_announcement_posted.side_effect = APIError(500, "boom")
        cog = _make_cog(api=api)
        announcement = _announcement()

        await cog._post_one(guild, channel, announcement)  # must not raise

        channel.send.assert_awaited_once()
        api.mark_announcement_posted.assert_awaited_once()

    async def test_discord_send_failure_leaves_it_unposted_and_never_acks(self) -> None:
        channel = _fake_channel()
        channel.send.side_effect = discord.HTTPException(MagicMock(), "boom")
        guild = _fake_guild(channel=channel)
        api = AsyncMock()
        cog = _make_cog(api=api)

        await cog._post_one(guild, channel, _announcement())  # must not raise

        api.mark_announcement_posted.assert_not_awaited()


class TestCheckLoop:
    async def test_not_found_does_not_permanently_exclude_the_guild(self) -> None:
        guild = _fake_guild(channel=_fake_channel())
        api = AsyncMock()
        api.list_pending_announcements.side_effect = [NotFoundError(404, "not found"), []]
        cog = _make_cog(api=api, guilds=[guild])

        await cog._check_loop.coro(cog)
        await cog._check_loop.coro(cog)

        assert api.list_pending_announcements.await_count == 2

    async def test_api_error_does_not_crash_the_loop(self) -> None:
        guild = _fake_guild(channel=_fake_channel())
        api = AsyncMock()
        api.list_pending_announcements.side_effect = APIError(500, "boom")
        cog = _make_cog(api=api, guilds=[guild])

        await cog._check_loop.coro(cog)  # must not raise

    async def test_multiple_pending_announcements_are_all_posted(self) -> None:
        channel = _fake_channel()
        guild = _fake_guild(channel=channel)
        api = AsyncMock()
        api.list_pending_announcements.return_value = [_announcement(), _announcement()]
        cog = _make_cog(api=api, guilds=[guild])

        await cog._check_loop.coro(cog)

        assert channel.send.await_count == 2
        assert api.mark_announcement_posted.await_count == 2
