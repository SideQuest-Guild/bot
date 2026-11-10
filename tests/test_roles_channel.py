"""Unit tests for the #roles channel plugin: rendering, channel placement, and message sync."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from sidequest_bot.api_client import APIError
from sidequest_bot.plugins import roles_channel
from sidequest_bot.plugins.roles_channel import RolesChannelCog, render_posts
from sidequest_common.schemas import BADGE_LIST, LevelRoleSchema
from sidequest_common.settings import WebSettings
from tests import fakes
from tests.fakes import named, sidequest_server

EXTRA_ROLES = (
    "Expedition Leader",
    "Seasoned Adventurer",
    "Adventurer",
    "Alternate Path",
    "NPC",
    "PUG",
    "First Raid",
    "Raid Veteran",
    "Perfect Month",
    "Founding Member",
    "Chatterbox",
    "Top Helper",
    "Party Animal",
    "Party Leader",
    "Open Road",
    "Crossroads Host",
    "Caravan Master",
)
"""Role names the real server has that the base fixture doesn't, so every BADGE_LIST entry resolves to a role."""


def _web_settings(guild_id: int) -> WebSettings:
    return WebSettings(_env_file=None, GUILD_ID=guild_id)


def _config(
    *,
    roster_role_id: int | None = None,
    gate_role_id: int | None = None,
    linked_role_id: int | None = None,
    battlenet_role_id: int | None = None,
    away_role_id: int | None = None,
    pug_role_id: int | None = None,
    badge_role_ids: dict[str, int] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        roster_role_id=roster_role_id,
        gate_role_id=gate_role_id,
        linked_role_id=linked_role_id,
        battlenet_role_id=battlenet_role_id,
        away_role_id=away_role_id,
        pug_role_id=pug_role_id,
        badge_role_ids=badge_role_ids or {},
    )


def _make_cog(*, api: object, guild: object, configs: object) -> RolesChannelCog:
    cog = RolesChannelCog.__new__(RolesChannelCog)
    cog.bot = SimpleNamespace(
        api=api,
        configs=configs,
        user=SimpleNamespace(id=fakes.BOT_USER_ID),
        get_guild=lambda gid: guild if gid == guild.id else None,
    )
    return cog


async def _add_addons_channel(guild: object) -> object:
    """Model ``#addons`` already existing (created by the ``addons_channel`` plugin on an earlier tick)."""
    rules = named(guild.channels, "rules")
    addons = await guild.create_text_channel("addons", overwrites=dict(rules.overwrites))
    await addons.move(after=rules)
    return addons


class TestRenderPosts:
    def test_includes_every_badge_in_badge_list(self) -> None:
        guild = sidequest_server(extra_roles=EXTRA_ROLES)
        posts = render_posts(guild, _config(), [])
        combined = "\n".join(post.embed.description for post in posts if post.embed is not None)
        for badge in BADGE_LIST:
            assert badge.description in combined

    def test_resolves_configured_role_id_over_exact_name(self) -> None:
        guild = sidequest_server()
        new_arrival = named(guild.roles, "New Arrival")
        posts = render_posts(guild, _config(roster_role_id=new_arrival.id), [])
        ranks = next(p.embed for p in posts if p.embed is not None and p.embed.title == "Guild ranks")
        assert new_arrival.mention in ranks.description

    def test_falls_back_to_exact_name_when_unconfigured(self) -> None:
        guild = sidequest_server()
        quest_master = named(guild.roles, "Quest Master")
        posts = render_posts(guild, _config(), [])
        ranks = next(p.embed for p in posts if p.embed is not None and p.embed.title == "Guild ranks")
        assert quest_master.mention in ranks.description

    def test_resolves_badge_role_ids(self) -> None:
        guild = sidequest_server(extra_roles=("Open Road",))
        open_road = named(guild.roles, "Open Road")
        posts = render_posts(guild, _config(badge_role_ids={"open_road": open_road.id}), [])
        badges = next(p.embed for p in posts if p.embed is not None and p.embed.title == "Hall of Heroes badges")
        assert open_road.mention in badges.description

    def test_missing_role_falls_back_to_bold_name(self) -> None:
        guild = sidequest_server()
        posts = render_posts(guild, _config(), [])
        badges = next(p.embed for p in posts if p.embed is not None and p.embed.title == "Hall of Heroes badges")
        assert "**Open Road**" in badges.description

    def test_level_rewards_show_reached_level(self) -> None:
        guild = sidequest_server()
        wayfarer = named(guild.roles, "Lv 5 · Wayfarer")
        posts = render_posts(guild, _config(), [LevelRoleSchema(level=5, role_id=wayfarer.id)])
        automatic = next(p.embed for p in posts if p.embed is not None and p.embed.title == "Automatic roles")
        assert f"{wayfarer.mention}: Reached level 5." in automatic.description

    def test_pug_role_resolves_from_config(self) -> None:
        guild = sidequest_server(extra_roles=("PUG",))
        pug = named(guild.roles, "PUG")
        posts = render_posts(guild, _config(pug_role_id=pug.id), [])
        automatic = next(p.embed for p in posts if p.embed is not None and p.embed.title == "Automatic roles")
        assert pug.mention in automatic.description


class TestCheckLoop:
    async def test_creates_roles_channel_below_addons_with_addons_overwrites(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        guild = sidequest_server()
        addons = await _add_addons_channel(guild)
        monkeypatch.setattr(roles_channel, "_web_settings", lambda: _web_settings(guild.id))
        api = AsyncMock()
        api.list_level_roles.return_value = []
        configs = AsyncMock()
        configs.get.return_value = _config()
        cog = _make_cog(api=api, guild=guild, configs=configs)

        await cog._check_loop.coro(cog)

        channel = named(guild.channels, "roles")
        assert channel.created_with["overwrites"] == addons.overwrites
        assert channel.created_with["overwrites"] is not addons.overwrites
        assert guild.channels.index(channel) == guild.channels.index(addons) + 1

    async def test_does_nothing_outside_the_site_guild(self, monkeypatch: pytest.MonkeyPatch) -> None:
        guild = sidequest_server()
        await _add_addons_channel(guild)
        monkeypatch.setattr(roles_channel, "_web_settings", lambda: _web_settings(guild.id + 1))
        api = AsyncMock()
        configs = AsyncMock()
        cog = _make_cog(api=api, guild=guild, configs=configs)

        await cog._check_loop.coro(cog)

        configs.get.assert_not_called()
        assert named(guild.channels, "addons") is not None
        assert [c for c in guild.channels if c.name.casefold() == "roles"] == []

    async def test_second_tick_with_unchanged_state_sends_or_edits_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        guild = sidequest_server()
        await _add_addons_channel(guild)
        monkeypatch.setattr(roles_channel, "_web_settings", lambda: _web_settings(guild.id))
        api = AsyncMock()
        api.list_level_roles.return_value = []
        configs = AsyncMock()
        configs.get.return_value = _config()
        cog = _make_cog(api=api, guild=guild, configs=configs)
        await cog._check_loop.coro(cog)
        channel = named(guild.channels, "roles")
        channel.send.reset_mock()

        await cog._check_loop.coro(cog)

        channel.send.assert_not_called()
        for message in channel.messages:
            message.edit.assert_not_called()

    async def test_posts_never_ping(self, monkeypatch: pytest.MonkeyPatch) -> None:
        guild = sidequest_server()
        await _add_addons_channel(guild)
        monkeypatch.setattr(roles_channel, "_web_settings", lambda: _web_settings(guild.id))
        api = AsyncMock()
        api.list_level_roles.return_value = []
        configs = AsyncMock()
        configs.get.return_value = _config()
        cog = _make_cog(api=api, guild=guild, configs=configs)

        await cog._check_loop.coro(cog)

        channel = named(guild.channels, "roles")
        none_mentions = discord.AllowedMentions.none().to_dict()
        for call in channel.send.await_args_list:
            assert call.kwargs["allowed_mentions"].to_dict() == none_mentions

    async def test_config_error_does_not_crash_the_loop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        guild = sidequest_server()
        await _add_addons_channel(guild)
        monkeypatch.setattr(roles_channel, "_web_settings", lambda: _web_settings(guild.id))
        api = AsyncMock()
        configs = AsyncMock()
        configs.get.side_effect = APIError(500, "boom")
        cog = _make_cog(api=api, guild=guild, configs=configs)

        await cog._check_loop.coro(cog)  # must not raise

        assert [c for c in guild.channels if c.name.casefold() == "roles"] == []

    async def test_no_addons_channel_does_not_crash_the_loop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        guild = sidequest_server()
        monkeypatch.setattr(roles_channel, "_web_settings", lambda: _web_settings(guild.id))
        api = AsyncMock()
        api.list_level_roles.return_value = []
        configs = AsyncMock()
        configs.get.return_value = _config()
        cog = _make_cog(api=api, guild=guild, configs=configs)

        await cog._check_loop.coro(cog)  # must not raise

        assert [c for c in guild.channels if c.name.casefold() == "roles"] == []
