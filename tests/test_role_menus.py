import asyncio
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import discord
import pytest

from sidequest_bot.plugins import role_menus
from sidequest_bot.plugins.role_menus import (
    CUSTOM_ID_RE,
    RoleMenuButton,
    RoleMenusCog,
    build_custom_id,
    build_menu_view,
    exceeds_max_roles,
    parse_custom_id,
    render_menu_embed,
)
from sidequest_common.schemas import RoleMenuOptionSchema, RoleMenuSchema


def _make_menu(
    options: list[RoleMenuOptionSchema], *, description: str | None = None, max_roles: int = 0
) -> RoleMenuSchema:
    return RoleMenuSchema(
        id=uuid4(),
        guild_id=1,
        channel_id=2,
        message_id=None,
        title="Pick your roles",
        description=description,
        max_roles=max_roles,
        options=options,
    )


class TestCustomId:
    def test_round_trips(self) -> None:
        menu_id = uuid4()
        custom_id = build_custom_id(menu_id, 123456789012345678)
        assert parse_custom_id(custom_id) == (menu_id, 123456789012345678)

    def test_matches_discord_id_template(self) -> None:
        custom_id = build_custom_id(uuid4(), 1)
        assert CUSTOM_ID_RE.match(custom_id) is not None

    @pytest.mark.parametrize(
        "garbage",
        [
            "",
            "garbage",
            "sq:rm:not-a-uuid:123",
            f"sq:lvl:{uuid4()}:123",  # another plugin's prefix
            f"sq:rm:{uuid4()}:abc",  # non-numeric role id
            f"sq:rm:{uuid4()}",  # missing role id
            f"sq:rm:{uuid4()}:123:extra",
            f"sq:rm:{uuid4()}:-1",  # negative role id
        ],
    )
    def test_rejects_garbage(self, garbage: str) -> None:
        assert parse_custom_id(garbage) is None


class TestExceedsMaxRoles:
    def test_unlimited_never_blocks(self) -> None:
        assert exceeds_max_roles(has_role=False, max_roles=0, held_count=1000) is False

    def test_removing_a_held_role_never_blocks(self) -> None:
        assert exceeds_max_roles(has_role=True, max_roles=1, held_count=5) is False

    def test_under_cap_is_allowed(self) -> None:
        assert exceeds_max_roles(has_role=False, max_roles=2, held_count=1) is False

    def test_at_cap_is_blocked(self) -> None:
        assert exceeds_max_roles(has_role=False, max_roles=2, held_count=2) is True

    def test_over_cap_is_blocked(self) -> None:
        assert exceeds_max_roles(has_role=False, max_roles=1, held_count=5) is True

    def test_zero_held_under_nonzero_cap_is_allowed(self) -> None:
        assert exceeds_max_roles(has_role=False, max_roles=1, held_count=0) is False


class TestRenderMenuEmbed:
    def test_one_line_per_option_in_position_order(self) -> None:
        menu = _make_menu(
            [
                RoleMenuOptionSchema(role_id=101, label="A", emoji="🎮", description="games", position=0),
                RoleMenuOptionSchema(role_id=102, label="B", emoji=None, description=None, position=1),
            ],
            description="Choose wisely",
        )
        embed = render_menu_embed(menu)
        assert embed.title == "Pick your roles"
        assert embed.description is not None
        assert "Choose wisely" in embed.description
        lines = embed.description.splitlines()
        assert "🎮 <@&101> — games" in lines
        assert "<@&102>" in lines
        assert lines.index("🎮 <@&101> — games") < lines.index("<@&102>")

    def test_empty_menu_has_no_option_lines(self) -> None:
        menu = _make_menu([], description="Nothing yet")
        embed = render_menu_embed(menu)
        assert embed.description == "Nothing yet"

    def test_max_size_inputs_stay_within_discord_description_limit(self) -> None:
        long_emoji = "<:" + "x" * 56 + ":123456789012345678>"  # schema caps emoji at 64 chars
        options = [
            RoleMenuOptionSchema(
                role_id=1000 + i, label=f"Role {i}", emoji=long_emoji, description="d" * 100, position=i
            )
            for i in range(25)  # the API's option cap
        ]
        menu = _make_menu(options, description="x" * 4000)  # the schema's description cap
        embed = render_menu_embed(menu)
        assert embed.description is not None
        assert len(embed.description) <= 4096


class TestBuildMenuView:
    def test_one_button_per_option_in_position_order(self) -> None:
        menu = _make_menu(
            [
                RoleMenuOptionSchema(role_id=101, label="A", emoji=None, description=None, position=0),
                RoleMenuOptionSchema(role_id=102, label="B", emoji=None, description=None, position=1),
                RoleMenuOptionSchema(role_id=103, label="C", emoji=None, description=None, position=2),
            ],
        )
        view = build_menu_view(menu)
        assert len(view.children) == 3
        assert [item.role_id for item in view.children] == [101, 102, 103]
        assert all(isinstance(item, RoleMenuButton) for item in view.children)
        assert all(item.item.style == discord.ButtonStyle.secondary for item in view.children)

    def test_options_count_matches_button_count(self) -> None:
        menu = _make_menu([RoleMenuOptionSchema(role_id=1, label="Solo", emoji=None, description=None, position=0)])
        view = build_menu_view(menu)
        assert len(view.children) == 1


class TestRoleMenuButtonFromCustomId:
    async def test_rebuilds_with_live_label_and_emoji(self) -> None:
        menu_id = uuid4()
        item = Mock(custom_id=build_custom_id(menu_id, 42), label="Gamer", emoji="🎮")
        button = await RoleMenuButton.from_custom_id(Mock(), item, Mock())
        assert button.menu_id == menu_id
        assert button.role_id == 42
        assert button.item.label == "Gamer"

    async def test_raises_for_unroutable_custom_id(self) -> None:
        item = Mock(custom_id="garbage", label=None, emoji=None)
        with pytest.raises(ValueError, match="Unroutable"):
            await RoleMenuButton.from_custom_id(Mock(), item, Mock())


def _make_offering_menu(role_id: int, *, max_roles: int = 0) -> RoleMenuSchema:
    return _make_menu(
        [RoleMenuOptionSchema(role_id=role_id, label="A", emoji=None, description=None, position=0)],
        max_roles=max_roles,
    )


class TestRoleMenuButtonCapUnderLock:
    """The max_roles cap must hold even when two clicks race: see _resolve_and_toggle's lock."""

    async def test_concurrent_clicks_for_the_same_member_are_serialized(self) -> None:
        menu = _make_offering_menu(101)
        bot = Mock()
        bot.api.get_role_menu = AsyncMock(return_value=menu)
        guild = Mock(spec=discord.Guild)
        guild.id = 555
        role = Mock(spec=discord.Role)
        role.id = 101
        role.mention = "@Role"
        guild.get_role = Mock(return_value=role)

        member = Mock(spec=discord.Member)
        member.roles = []
        member.guild = guild
        member.add_roles = AsyncMock()
        member.remove_roles = AsyncMock()

        active = 0
        max_active = 0

        async def fetch_member(_member_id: int) -> Mock:
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            return member

        guild.fetch_member = AsyncMock(side_effect=fetch_member)
        button = RoleMenuButton(menu.id, 101, label="A", emoji=None)

        with patch.object(role_menus, "grant_block_reason", return_value=None):
            await asyncio.gather(
                button._resolve_and_toggle(bot, guild, 999),
                button._resolve_and_toggle(bot, guild, 999),
            )
        assert max_active == 1

    async def test_cap_check_uses_freshly_fetched_role_state(self) -> None:
        """A member already holding a role from the menu (per the authoritative fetch) can't exceed the cap."""
        option_a = RoleMenuOptionSchema(role_id=101, label="A", emoji=None, description=None, position=0)
        option_b = RoleMenuOptionSchema(role_id=102, label="B", emoji=None, description=None, position=1)
        menu = _make_menu([option_a, option_b], max_roles=1)
        bot = Mock()
        bot.api.get_role_menu = AsyncMock(return_value=menu)

        guild = Mock(spec=discord.Guild)
        guild.id = 1
        role_a = Mock(spec=discord.Role, id=101)
        role_b = Mock(spec=discord.Role, id=102)
        guild.get_role = Mock(side_effect={101: role_a, 102: role_b}.get)

        member = Mock(spec=discord.Member)
        member.roles = [role_a]  # already holds one role from this menu
        member.guild = guild
        guild.fetch_member = AsyncMock(return_value=member)

        button = RoleMenuButton(menu.id, 102, label="B", emoji=None)
        with patch.object(role_menus, "grant_block_reason", return_value=None):
            message = await button._resolve_and_toggle(bot, guild, 999)
        assert "can only hold 1" in message


def _make_cog_interaction(*, guild_id: int = 1) -> Mock:
    interaction = Mock(spec=discord.Interaction)
    interaction.guild = Mock(spec=discord.Guild)
    interaction.guild.id = guild_id
    interaction.user = Mock(spec=discord.Member)
    interaction.response = Mock()
    interaction.response.defer = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.followup = Mock()
    interaction.followup.send = AsyncMock()
    return interaction


class TestAddCommandRequiresGrantAndConfigureChecks:
    """Offering a role takes two independent checks: the bot must be able to grant it, and the inviting member must
    be allowed to configure it - otherwise a Manage Roles member could offer (and self-assign) a role above their
    own and escalate through the bot's permissions.
    """

    async def test_blocks_when_role_is_ungrantable_even_if_member_may_configure(self) -> None:
        bot = Mock()
        cog = RoleMenusCog(bot)
        interaction = _make_cog_interaction()
        role = Mock(spec=discord.Role)
        with (
            patch.object(role_menus, "grant_block_reason", return_value="too dangerous"),
            patch.object(role_menus, "configure_block_reason", return_value=None),
        ):
            await RoleMenusCog.add.callback(cog, interaction, "menu-id", role, "Label")
        interaction.response.send_message.assert_awaited_once()
        assert "too dangerous" in interaction.response.send_message.await_args.args[0]
        bot.api.upsert_role_menu_option.assert_not_called()

    async def test_blocks_when_member_may_not_configure_even_if_role_is_grantable(self) -> None:
        bot = Mock()
        cog = RoleMenusCog(bot)
        interaction = _make_cog_interaction()
        role = Mock(spec=discord.Role)
        with (
            patch.object(role_menus, "grant_block_reason", return_value=None),
            patch.object(role_menus, "configure_block_reason", return_value="above your rank"),
        ):
            await RoleMenusCog.add.callback(cog, interaction, "menu-id", role, "Label")
        interaction.response.send_message.assert_awaited_once()
        assert "above your rank" in interaction.response.send_message.await_args.args[0]
        bot.api.upsert_role_menu_option.assert_not_called()

    async def test_proceeds_when_both_checks_pass(self) -> None:
        bot = Mock()
        updated = _make_offering_menu(101)
        bot.api.upsert_role_menu_option = AsyncMock(return_value=updated)
        cog = RoleMenusCog(bot)
        interaction = _make_cog_interaction()
        role = Mock(spec=discord.Role)
        role.id = 101
        role.mention = "@Role"
        with (
            patch.object(role_menus, "grant_block_reason", return_value=None),
            patch.object(role_menus, "configure_block_reason", return_value=None),
            patch.object(role_menus, "_sync_menu_message", new=AsyncMock(return_value=updated)),
        ):
            await RoleMenusCog.add.callback(cog, interaction, str(uuid4()), role, "Label")
        bot.api.upsert_role_menu_option.assert_awaited_once()
        interaction.followup.send.assert_awaited_once()
