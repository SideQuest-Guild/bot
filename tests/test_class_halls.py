"""Class halls: a role's members follow its "<role> Hall" forum post; the sweep only ever adds people."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from sidequest_bot.lib.class_guides import CLASS_GUIDES, WOW_CLASSES
from sidequest_bot.plugins.class_halls import ClassHallsCog, guides_embed, guides_view, view_buttons

GUILD_ID = 42
BOT_ID = 1
WARRIOR, MAGE, OTHER = 10, 11, 12


def _role(role_id: int, name: str, members: list[object] | None = None) -> MagicMock:
    role = MagicMock(spec=discord.Role)
    role.id = role_id
    role.name = name
    role.members = members or []
    role.is_default.return_value = False
    return role


def _messages(messages: list[object]):
    async def _iterate(**_: object):
        for message in messages:
            yield message

    return _iterate


def _hall(
    thread_id: int,
    name: str,
    *,
    archived: bool = False,
    following: list[int] | None = None,
    pins: list[object] | None = None,
) -> MagicMock:
    hall = MagicMock(spec=discord.Thread)
    hall.id = thread_id
    hall.name = name
    hall.archived = archived
    hall.parent = MagicMock(spec=discord.ForumChannel)
    hall.add_user = AsyncMock()
    hall.remove_user = AsyncMock()
    hall.edit = AsyncMock(side_effect=lambda **changes: setattr(hall, "archived", changes["archived"]))
    hall.fetch_members = AsyncMock(return_value=[SimpleNamespace(id=member_id) for member_id in following or []])
    hall.pins = _messages(pins or [])
    hall.history = _messages([])
    hall.send = AsyncMock(return_value=MagicMock(pin=AsyncMock()))
    return hall


def _member(member_id: int, roles: list[MagicMock], guild: MagicMock) -> SimpleNamespace:
    return SimpleNamespace(id=member_id, bot=False, roles=roles, guild=guild)


def _guild(roles: list[MagicMock], halls: list[MagicMock]) -> MagicMock:
    guild = MagicMock(spec=discord.Guild)
    guild.id = GUILD_ID
    guild.roles = roles
    guild.threads = halls
    guild.forums = []
    guild.get_role.side_effect = lambda role_id: next((r for r in roles if r.id == role_id), None)
    guild.get_thread.side_effect = lambda thread_id: next((h for h in halls if h.id == thread_id), None)
    return guild


def _cog(guild: MagicMock) -> ClassHallsCog:
    cog = ClassHallsCog.__new__(ClassHallsCog)
    cog.bot = SimpleNamespace(guilds=[guild], user=SimpleNamespace(id=BOT_ID))
    cog._hall_ids = {}
    return cog


def _guides_post(embed: discord.Embed, view: discord.ui.View) -> MagicMock:
    """A sent guides post as Discord hands it back: the embed, and the buttons inside action rows."""
    post = MagicMock(spec=discord.Message)
    post.author = SimpleNamespace(id=BOT_ID)
    post.embeds = [embed]
    row = MagicMock(spec=discord.ActionRow)
    row.children = []
    for label, url in view_buttons(view):
        button = MagicMock(spec=discord.Button)
        button.label, button.url = label, url
        row.children.append(button)
    post.components = [row]
    post.edit = AsyncMock()
    return post


async def test_gaining_a_class_follows_its_hall_and_losing_it_unfollows() -> None:
    warrior = _role(WARRIOR, "Warrior")
    hall = _hall(500, "Warrior Hall")
    guild = _guild([warrior], [hall])
    cog = _cog(guild)
    await cog._sweep_loop.coro(cog)
    member_before = _member(1, [], guild)
    member_after = _member(1, [warrior], guild)

    await cog.on_member_update(member_before, member_after)  # type: ignore[arg-type]
    hall.add_user.assert_awaited_once_with(member_after)

    await cog.on_member_update(member_after, member_before)  # type: ignore[arg-type]
    hall.remove_user.assert_awaited_once_with(member_before)


async def test_role_without_a_hall_is_ignored() -> None:
    other = _role(OTHER, "Signed the Ledger")
    hall = _hall(500, "Warrior Hall")
    guild = _guild([other], [hall])
    cog = _cog(guild)
    await cog._sweep_loop.coro(cog)

    await cog.on_member_update(_member(1, [], guild), _member(1, [other], guild))  # type: ignore[arg-type]

    hall.add_user.assert_not_awaited()
    hall.remove_user.assert_not_awaited()


async def test_archived_hall_is_reopened_before_adding() -> None:
    mage = _role(MAGE, "Mage")
    hall = _hall(501, "Mage Hall", archived=True)
    guild = _guild([mage], [hall])
    cog = _cog(guild)
    await cog._sweep_loop.coro(cog)
    hall.archived = True  # it archived itself again after the sweep posted the guides
    hall.edit.reset_mock()

    await cog.on_member_update(_member(2, [], guild), _member(2, [mage], guild))  # type: ignore[arg-type]

    hall.edit.assert_awaited_once_with(archived=False)
    hall.add_user.assert_awaited_once()


async def test_sweep_adds_missing_holders_and_never_removes_browsers() -> None:
    guild = _guild([], [])
    holder, already, browser = (_member(member_id, [], guild) for member_id in (1, 2, 3))
    warrior = _role(WARRIOR, "Warrior", members=[holder, already])
    hall = _hall(500, "Warrior Hall", following=[2, 3])
    guild.roles = [warrior]
    guild.threads = [hall]
    guild.get_member.side_effect = {1: holder, 2: already, 3: browser}.get
    cog = _cog(guild)

    await cog._sweep_loop.coro(cog)

    hall.add_user.assert_awaited_once_with(holder)
    hall.remove_user.assert_not_awaited()


async def test_failed_add_does_not_raise() -> None:
    warrior = _role(WARRIOR, "Warrior")
    hall = _hall(500, "Warrior Hall")
    hall.add_user.side_effect = discord.HTTPException(MagicMock(status=403), "Missing Access")
    guild = _guild([warrior], [hall])
    cog = _cog(guild)
    await cog._sweep_loop.coro(cog)

    await cog.on_member_update(_member(1, [], guild), _member(1, [warrior], guild))  # type: ignore[arg-type]

    hall.add_user.assert_awaited_once()


class TestGuides:
    async def test_sweep_posts_and_pins_guides_only_in_class_halls(self) -> None:
        warrior_hall = _hall(500, "Warrior Hall", archived=True)
        other_hall = _hall(502, "Raid Leaders Hall")
        cog = _cog(_guild([], [warrior_hall, other_hall]))

        await cog._sweep_loop.coro(cog)

        warrior_hall.edit.assert_awaited_once_with(archived=False)
        sent = warrior_hall.send.await_args.kwargs
        assert sent["embed"].title == "Recommended Warrior guides"
        assert view_buttons(sent["view"]) == [
            (f"{guide.site} · {guide.label}", guide.url) for guide in CLASS_GUIDES["Warrior"]
        ]
        warrior_hall.send.return_value.pin.assert_awaited_once()
        other_hall.send.assert_not_awaited()

    async def test_current_guides_post_is_left_alone(self) -> None:
        post = _guides_post(guides_embed("Mage"), guides_view("Mage"))
        hall = _hall(501, "Mage Hall", archived=True, pins=[post])
        cog = _cog(_guild([], [hall]))

        await cog._sweep_loop.coro(cog)

        hall.send.assert_not_awaited()
        hall.edit.assert_not_awaited()
        post.edit.assert_not_awaited()

    async def test_changed_guide_list_edits_the_post_in_place(self) -> None:
        outdated = discord.ui.View()
        outdated.add_item(discord.ui.Button(label="Method · Frost", url="https://example.com/old-frost"))
        post = _guides_post(guides_embed("Mage"), outdated)
        hall = _hall(501, "Mage Hall", pins=[post])
        cog = _cog(_guild([], [hall]))

        await cog._sweep_loop.coro(cog)

        post.edit.assert_awaited_once()
        assert view_buttons(post.edit.await_args.kwargs["view"]) == view_buttons(guides_view("Mage"))
        hall.send.assert_not_awaited()

    async def test_new_class_hall_gets_its_guides_right_away(self) -> None:
        hall = _hall(503, "Druid Hall")
        hall.guild = SimpleNamespace(id=GUILD_ID)
        cog = _cog(_guild([], []))

        await cog.on_thread_create(hall)

        assert cog.has_halls(GUILD_ID)
        assert hall.send.await_args.kwargs["embed"].title == "Recommended Druid guides"


@pytest.mark.parametrize("wow_class", WOW_CLASSES)
def test_every_class_has_guides_that_fit_one_row_per_site(wow_class: str) -> None:
    view = guides_view(wow_class)  # raises if a site's guides overflow its row of five buttons
    assert len(view_buttons(view)) == len(CLASS_GUIDES[wow_class])
