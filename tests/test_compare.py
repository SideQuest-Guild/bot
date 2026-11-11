"""Unit tests for the compare plugin: formatting helpers, the gear table, embed builder and ``/compare``."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord

from sidequest_bot.api_client import APIError, CharacterGearItem, CharacterPage, CharacterSheet, NotFoundError
from sidequest_bot.plugins.characters import NAME_HINT
from sidequest_bot.plugins.compare import (
    UNAVAILABLE_MESSAGE,
    CompareCog,
    build_compare_embed,
    build_gear_table,
    character_field_value,
    gear_by_slot,
    gear_diff_text,
    item_level_value,
)


def _page(**overrides: object) -> CharacterPage:
    base: dict[str, object] = {
        "slug": "nox-zul",
        "name": "Nox",
        "surname": "Zul",
        "level": 60,
        "class": "mage",
        "race": "Gnome",
        "faction": "Alliance",
        "guild_name": "SideQuest",
        "guild_slug": "sidequest",
        "in_roster": True,
        "guild_rank": 0,
        "rank_name": "Guild Master",
        "item_level": 70,
        "spec": "Frost",
        "achievement_points": 500,
        "honorable_kills": 10,
        "pvp_rank": 3,
        "last_login_at": None,
        "profile_found": True,
        "fetched_at": None,
        "sheet": None,
    }
    base.update(overrides)
    return CharacterPage.model_validate(base)


def _gear(*, slot_name: str = "Head", name: str = "Lionheart Helm", item_level: int | None = 65) -> CharacterGearItem:
    return CharacterGearItem(slot_name=slot_name, name=name, item_level=item_level)


def _make_cog(*, api: object | None = None) -> CompareCog:
    cog = CompareCog.__new__(CompareCog)
    cog.bot = SimpleNamespace(api=api or AsyncMock())
    return cog


def _make_interaction(*, api: object | None = None) -> Mock:
    interaction = Mock(spec=discord.Interaction)
    interaction.response = Mock()
    interaction.response.defer = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.followup = Mock()
    interaction.followup.send = AsyncMock()
    interaction.client = SimpleNamespace(api=api or AsyncMock())
    return interaction


class TestItemLevelValue:
    def test_both_known_highlights_leader(self) -> None:
        a = _page(name="Nox", item_level=70)
        b = _page(name="Vex", item_level=65)
        assert item_level_value(a, b) == "70 vs 65 (Nox +5)"

    def test_second_leads(self) -> None:
        a = _page(name="Nox", item_level=60)
        b = _page(name="Vex", item_level=65)
        assert item_level_value(a, b) == "60 vs 65 (Vex +5)"

    def test_tied(self) -> None:
        a = _page(item_level=70)
        b = _page(item_level=70)
        assert item_level_value(a, b) == "70 vs 70 (tied)"

    def test_missing_side_shows_dash(self) -> None:
        a = _page(item_level=None)
        b = _page(item_level=65)
        assert item_level_value(a, b) == "— vs 65"


class TestCharacterFieldValue:
    def test_combines_description_and_pvp(self) -> None:
        value = character_field_value(_page())
        assert "Level 60 Gnome Mage · Frost" in value
        assert "10 honorable kills · PvP rank 3" in value


class TestGearBySlot:
    def test_no_sheet_returns_empty(self) -> None:
        assert gear_by_slot(_page(sheet=None)) == {}

    def test_sheet_gear_maps_slot_to_item_level(self) -> None:
        page = _page(sheet=CharacterSheet(gear=[_gear(slot_name="Head", item_level=65)]))
        assert gear_by_slot(page) == {"Head": 65}


class TestGearDiffText:
    def test_positive_diff(self) -> None:
        assert gear_diff_text(70, 65) == "+5"

    def test_negative_diff(self) -> None:
        assert gear_diff_text(60, 65) == "-5"

    def test_zero_diff(self) -> None:
        assert gear_diff_text(65, 65) == "0"

    def test_missing_side_shows_dash(self) -> None:
        assert gear_diff_text(None, 65) == "—"
        assert gear_diff_text(65, None) == "—"


class TestBuildGearTable:
    def test_no_gear_shows_placeholder(self) -> None:
        table = build_gear_table(_page(sheet=None), _page(sheet=None))
        assert table == "No gear data for either character yet."

    def test_includes_every_slot_from_both_sides(self) -> None:
        a = _page(sheet=CharacterSheet(gear=[_gear(slot_name="Head", item_level=65)]))
        b = _page(sheet=CharacterSheet(gear=[_gear(slot_name="Chest", item_level=60)]))
        table = build_gear_table(a, b)
        assert "Head" in table
        assert "Chest" in table

    def test_missing_side_shows_dash_for_that_slot(self) -> None:
        a = _page(sheet=CharacterSheet(gear=[_gear(slot_name="Head", item_level=65)]))
        b = _page(sheet=CharacterSheet(gear=[]))
        table = build_gear_table(a, b)
        head_line = next(line for line in table.splitlines() if line.startswith("Head"))
        assert "—" in head_line


class TestBuildCompareEmbed:
    def test_title_and_fields(self) -> None:
        a = _page(name="Nox", surname="Zul")
        b = _page(name="Vex", surname="Shade")
        embed = build_compare_embed(a, b)
        assert embed.title == "Nox Zul vs Vex Shade"
        field_names = [field.name for field in embed.fields]
        assert "Nox Zul" in field_names
        assert "Vex Shade" in field_names
        assert "Item level" in field_names
        assert "Gear by slot" in field_names

    def test_gear_field_is_a_code_block(self) -> None:
        embed = build_compare_embed(_page(), _page())
        gear_field = next(field for field in embed.fields if field.name == "Gear by slot")
        assert gear_field.value is not None
        assert gear_field.value.startswith("```")
        assert gear_field.value.endswith("```")


class TestCompareCommand:
    async def test_invalid_first_character_sends_hint(self) -> None:
        cog = _make_cog()
        interaction = _make_interaction()

        await CompareCog.compare_command.callback(cog, interaction, "NoxZul", "Vex Shade")

        interaction.response.send_message.assert_awaited_once_with(NAME_HINT, ephemeral=True)

    async def test_invalid_second_character_sends_hint(self) -> None:
        cog = _make_cog()
        interaction = _make_interaction()

        await CompareCog.compare_command.callback(cog, interaction, "Nox Zul", "VexShade")

        interaction.response.send_message.assert_awaited_once_with(NAME_HINT, ephemeral=True)

    async def test_success_sends_embed_with_both_characters(self) -> None:
        api = AsyncMock()
        api.get_character.side_effect = lambda slug: _page(
            name="Nox" if slug == "nox-zul" else "Vex", surname="Zul" if slug == "nox-zul" else "Shade", slug=slug
        )
        cog = _make_cog(api=api)
        interaction = _make_interaction(api=api)

        await CompareCog.compare_command.callback(cog, interaction, "Nox Zul", "Vex Shade")

        assert {call.args[0] for call in api.get_character.await_args_list} == {"nox-zul", "vex-shade"}
        kwargs = interaction.followup.send.await_args.kwargs
        assert kwargs["embed"].title == "Nox Zul vs Vex Shade"

    async def test_first_not_found_reports_api_detail(self) -> None:
        api = AsyncMock()

        async def side_effect(slug: str) -> CharacterPage:
            if slug == "nox-zul":
                raise NotFoundError(404, "Battle.net has no character nox-zul.")
            return _page(slug=slug)

        api.get_character.side_effect = side_effect
        cog = _make_cog(api=api)
        interaction = _make_interaction(api=api)

        await CompareCog.compare_command.callback(cog, interaction, "Nox Zul", "Vex Shade")

        interaction.followup.send.assert_awaited_once_with("Battle.net has no character nox-zul.", ephemeral=True)

    async def test_second_unavailable_reports_clear_message(self) -> None:
        api = AsyncMock()

        async def side_effect(slug: str) -> CharacterPage:
            if slug == "vex-shade":
                raise APIError(503, "Battle.net unavailable")
            return _page(slug=slug)

        api.get_character.side_effect = side_effect
        cog = _make_cog(api=api)
        interaction = _make_interaction(api=api)

        await CompareCog.compare_command.callback(cog, interaction, "Nox Zul", "Vex Shade")

        interaction.followup.send.assert_awaited_once_with(UNAVAILABLE_MESSAGE, ephemeral=True)
