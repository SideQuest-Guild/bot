"""Role icon emoji: every icon is a valid Discord emoji, and the bot only adds names the server lacks."""

import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from sidequest_bot.plugins.emoji import EmojiCog, plan_new_emoji, role_icon_emoji

EMOJI_NAME = re.compile(r"^[A-Za-z0-9_]{2,32}$")
EMOJI_MAX_BYTES = 256 * 1024
CLASSES = {"druid", "hunter", "mage", "paladin", "priest", "rogue", "shaman", "warlock", "warrior"}


def test_every_role_icon_is_an_emoji_discord_accepts() -> None:
    icons = role_icon_emoji()
    assert icons.keys() >= CLASSES
    for name, image in icons.items():
        assert EMOJI_NAME.match(name), name
        assert image.startswith(b"\x89PNG"), name
        assert len(image) <= EMOJI_MAX_BYTES, name


def test_plan_skips_names_the_server_has_in_any_case() -> None:
    to_create, no_room = plan_new_emoji(["mage", "rogue", "warrior"], {"warrior"}, free_slots=10)
    assert to_create == ["mage", "rogue"]
    assert no_room == []


def test_plan_stops_at_the_free_slots_and_reports_the_rest() -> None:
    assert plan_new_emoji(["a1", "b2", "c3"], set(), free_slots=2) == (["a1", "b2"], ["c3"])
    assert plan_new_emoji(["a1", "b2"], set(), free_slots=0) == ([], ["a1", "b2"])
    assert plan_new_emoji(["a1"], set(), free_slots=-3) == ([], ["a1"])


def _guild(emoji: list[tuple[str, bool]], *, limit: int = 150, can_create: bool = True) -> MagicMock:
    guild = MagicMock(spec=discord.Guild)
    guild.id = 42
    guild.emojis = [SimpleNamespace(name=name, animated=animated) for name, animated in emoji]
    guild.emoji_limit = limit
    guild.me = SimpleNamespace(
        guild_permissions=discord.Permissions(create_expressions=can_create, manage_expressions=False)
    )
    guild.create_custom_emoji = AsyncMock()
    return guild


async def test_sync_creates_only_the_missing_icons_and_leaves_existing_names_alone() -> None:
    names = list(role_icon_emoji())
    guild = _guild([("Warrior", False), ("party_parrot", True)])

    created = await EmojiCog.sync_guild(guild)

    assert created == [name for name in names if name != "warrior"]
    uploaded = {call.kwargs["name"]: call.kwargs["image"] for call in guild.create_custom_emoji.await_args_list}
    assert uploaded.keys() == set(created)
    assert uploaded["mage"] == role_icon_emoji()["mage"]


async def test_animated_emoji_dont_use_up_static_slots() -> None:
    names = list(role_icon_emoji())
    guild = _guild([(f"dance{i}", True) for i in range(50)], limit=len(names))

    assert await EmojiCog.sync_guild(guild) == names


async def test_sync_fills_the_last_free_slots_then_stops() -> None:
    guild = _guild([(f"custom{i}", False) for i in range(48)], limit=50)

    created = await EmojiCog.sync_guild(guild)

    assert created == list(role_icon_emoji())[:2]


async def test_one_failed_upload_doesnt_stop_the_rest() -> None:
    names = list(role_icon_emoji())
    guild = _guild([])
    response = SimpleNamespace(status=400, reason="Bad Request")
    guild.create_custom_emoji.side_effect = [discord.HTTPException(response, "bad image")] + [None] * (len(names) - 1)

    assert await EmojiCog.sync_guild(guild) == names[1:]


async def test_sync_does_nothing_without_the_permission() -> None:
    guild = _guild([], can_create=False)

    assert await EmojiCog.sync_guild(guild) == []
    guild.create_custom_emoji.assert_not_awaited()
