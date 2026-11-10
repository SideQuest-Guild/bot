"""Unit tests for the #addons channel plugin: rendering, channel placement, and message sync."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import discord
import pytest

from sidequest_bot.api_client import APIError, NotFoundError
from sidequest_bot.lib.channel_sync import find_text_channel
from sidequest_bot.plugins import addons_channel
from sidequest_bot.plugins.addons_channel import (
    AddonsChannelCog,
    build_intro,
    render_posts,
)
from sidequest_common.schemas import ADDON_CATEGORY_LABELS, AddonCategory, AddonOrigin, AddonSchema
from sidequest_common.settings import WebSettings
from tests import fakes
from tests.fakes import named, sidequest_server

BASE_URL = "https://sidequest-guild.com"


def _addon(**overrides: object) -> AddonSchema:
    base: dict[str, object] = {
        "id": uuid4(),
        "slug": "addon",
        "name": "Addon",
        "summary": "Does a thing.",
        "version": "1.0",
        "download_url": "https://example.com/addon",
        "homepage_url": None,
        "origin": AddonOrigin.COMMUNITY,
        "category": AddonCategory.GENERAL,
        "updated_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    base.update(overrides)
    return AddonSchema.model_validate(base)


def _web_settings(guild_id: int) -> WebSettings:
    return WebSettings(_env_file=None, GUILD_ID=guild_id, BASE_URL=BASE_URL)


def _make_cog(*, api: object, guild: object) -> AddonsChannelCog:
    cog = AddonsChannelCog.__new__(AddonsChannelCog)
    cog.bot = SimpleNamespace(
        api=api,
        user=SimpleNamespace(id=fakes.BOT_USER_ID),
        get_guild=lambda gid: guild if gid == guild.id else None,
    )
    return cog


class TestFindTextChannel:
    def test_finds_by_name_anywhere_in_the_guild(self) -> None:
        guild = sidequest_server()
        assert find_text_channel(guild, "rules") is named(guild.channels, "rules")

    def test_missing_channel_is_none(self) -> None:
        guild = sidequest_server()
        assert find_text_channel(guild, "addons") is None


class TestRenderPosts:
    def test_categories_in_contract_order_guild_made_first(self) -> None:
        addons = [
            _addon(name="Zebra", category=AddonCategory.RAID, origin=AddonOrigin.COMMUNITY),
            _addon(name="Alpha", category=AddonCategory.RAID, origin=AddonOrigin.GUILD),
            _addon(name="Leveler", category=AddonCategory.LEVELING, origin=AddonOrigin.COMMUNITY),
        ]
        posts = render_posts(addons, BASE_URL)
        assert posts[0].content == build_intro(BASE_URL)
        assert posts[0].embed is None
        # RAID precedes LEVELING in AddonCategory's declaration order; RAID_REQUIRED and GENERAL/PVP are empty.
        assert posts[1].embed is not None
        assert posts[1].embed.title == ADDON_CATEGORY_LABELS[AddonCategory.RAID]
        assert posts[1].embed.description.splitlines() == [
            "**[Alpha](https://example.com/addon)**: Does a thing.",
            "**[Zebra](https://example.com/addon)**: Does a thing.",
        ]
        assert posts[2].embed is not None
        assert posts[2].embed.title == ADDON_CATEGORY_LABELS[AddonCategory.LEVELING]
        assert len(posts) == 3

    def test_skips_empty_categories(self) -> None:
        posts = render_posts([_addon(category=AddonCategory.PVP)], BASE_URL)
        assert len(posts) == 2
        assert posts[1].embed is not None
        assert posts[1].embed.title == ADDON_CATEGORY_LABELS[AddonCategory.PVP]


class TestCheckLoop:
    async def test_creates_addons_channel_below_rules_with_rules_overwrites(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        guild = sidequest_server()
        rules = named(guild.channels, "rules")
        monkeypatch.setattr(addons_channel, "_web_settings", lambda: _web_settings(guild.id))
        api = AsyncMock()
        api.list_addons.return_value = [_addon(category=AddonCategory.GENERAL)]
        cog = _make_cog(api=api, guild=guild)

        await cog._check_loop.coro(cog)

        channel = named(guild.channels, "addons")
        assert channel.created_with["overwrites"] == rules.overwrites
        assert channel.created_with["overwrites"] is not rules.overwrites
        assert guild.channels.index(channel) == guild.channels.index(rules) + 1

    async def test_does_nothing_outside_the_site_guild(self, monkeypatch: pytest.MonkeyPatch) -> None:
        guild = sidequest_server()
        monkeypatch.setattr(addons_channel, "_web_settings", lambda: _web_settings(guild.id + 1))
        api = AsyncMock()
        cog = _make_cog(api=api, guild=guild)

        await cog._check_loop.coro(cog)

        api.list_addons.assert_not_called()
        assert find_text_channel(guild, "addons") is None

    async def test_second_tick_with_unchanged_addons_sends_or_edits_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        guild = sidequest_server()
        monkeypatch.setattr(addons_channel, "_web_settings", lambda: _web_settings(guild.id))
        api = AsyncMock()
        api.list_addons.return_value = [_addon(name="Alpha", category=AddonCategory.GENERAL)]
        cog = _make_cog(api=api, guild=guild)
        await cog._check_loop.coro(cog)
        channel = named(guild.channels, "addons")
        channel.send.reset_mock()

        await cog._check_loop.coro(cog)

        channel.send.assert_not_called()
        for message in channel.messages:
            message.edit.assert_not_called()

    async def test_changed_summary_edits_only_that_message(self, monkeypatch: pytest.MonkeyPatch) -> None:
        guild = sidequest_server()
        monkeypatch.setattr(addons_channel, "_web_settings", lambda: _web_settings(guild.id))
        alpha = _addon(name="Alpha", category=AddonCategory.GENERAL, summary="Old summary.")
        beta = _addon(name="Beta", category=AddonCategory.PVP, summary="Other summary.")
        api = AsyncMock()
        api.list_addons.return_value = [alpha, beta]
        cog = _make_cog(api=api, guild=guild)
        await cog._check_loop.coro(cog)
        channel = named(guild.channels, "addons")
        intro_msg, general_msg, pvp_msg = channel.messages

        api.list_addons.return_value = [
            _addon(name="Alpha", category=AddonCategory.GENERAL, summary="New summary!"),
            beta,
        ]
        await cog._check_loop.coro(cog)

        intro_msg.edit.assert_not_called()
        general_msg.edit.assert_awaited_once()
        pvp_msg.edit.assert_not_called()
        assert "New summary!" in general_msg.embeds[0].description

    async def test_posts_never_ping(self, monkeypatch: pytest.MonkeyPatch) -> None:
        guild = sidequest_server()
        monkeypatch.setattr(addons_channel, "_web_settings", lambda: _web_settings(guild.id))
        api = AsyncMock()
        api.list_addons.return_value = [_addon(category=AddonCategory.RAID)]
        cog = _make_cog(api=api, guild=guild)

        await cog._check_loop.coro(cog)

        channel = named(guild.channels, "addons")
        none_mentions = discord.AllowedMentions.none().to_dict()
        for call in channel.send.await_args_list:
            assert call.kwargs["allowed_mentions"].to_dict() == none_mentions

    async def test_empty_addon_list_leaves_the_channel_untouched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        guild = sidequest_server()
        monkeypatch.setattr(addons_channel, "_web_settings", lambda: _web_settings(guild.id))
        api = AsyncMock()
        api.list_addons.return_value = [_addon(category=AddonCategory.GENERAL)]
        cog = _make_cog(api=api, guild=guild)
        await cog._check_loop.coro(cog)
        channel = named(guild.channels, "addons")
        message_count = len(channel.messages)
        channel.send.reset_mock()

        api.list_addons.return_value = []
        await cog._check_loop.coro(cog)

        channel.send.assert_not_called()
        assert len(channel.messages) == message_count

    async def test_not_found_does_not_crash_the_loop_and_retries_next_tick(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        guild = sidequest_server()
        monkeypatch.setattr(addons_channel, "_web_settings", lambda: _web_settings(guild.id))
        api = AsyncMock()
        api.list_addons.side_effect = [NotFoundError(404, "not found"), [_addon(category=AddonCategory.GENERAL)]]
        cog = _make_cog(api=api, guild=guild)

        await cog._check_loop.coro(cog)
        assert find_text_channel(guild, "addons") is None
        await cog._check_loop.coro(cog)

        assert api.list_addons.await_count == 2
        assert find_text_channel(guild, "addons") is not None

    async def test_api_error_does_not_crash_the_loop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        guild = sidequest_server()
        monkeypatch.setattr(addons_channel, "_web_settings", lambda: _web_settings(guild.id))
        api = AsyncMock()
        api.list_addons.side_effect = APIError(500, "boom")
        cog = _make_cog(api=api, guild=guild)

        await cog._check_loop.coro(cog)  # must not raise
