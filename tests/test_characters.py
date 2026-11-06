"""Unit tests for the characters plugin: slug parsing, embed building, autocomplete and ``/who``."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord

from sidequest_bot.api_client import (
    APIError,
    CharacterGearItem,
    CharacterPage,
    CharacterSheet,
    CharacterSummary,
    NotFoundError,
)
from sidequest_bot.lib.embeds import Colors
from sidequest_bot.plugins.characters import (
    NAME_HINT,
    UNAVAILABLE_MESSAGE,
    CharactersCog,
    _autocomplete_character,
    build_character_embed,
    build_description,
    build_gear_field,
    character_choice,
    class_color,
    gear_line,
    guild_value,
    pvp_value,
    resolve_slug,
)


def _summary(**overrides: object) -> CharacterSummary:
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
        "last_login_at": "2026-01-01T00:00:00+00:00",
        "profile_found": True,
        "fetched_at": "2026-01-01T00:00:00+00:00",
    }
    base.update(overrides)
    return CharacterSummary.model_validate(base)


def _gear(*, slot_name: str = "Head", name: str = "Lionheart Helm", item_level: int | None = 65) -> CharacterGearItem:
    return CharacterGearItem(slot_name=slot_name, name=name, item_level=item_level)


def _page(**overrides: object) -> CharacterPage:
    summary = _summary(**{k: v for k, v in overrides.items() if k != "sheet"})
    return CharacterPage(**summary.model_dump(by_alias=True), sheet=overrides.get("sheet"))


def _make_cog(api: object) -> CharactersCog:
    cog = CharactersCog.__new__(CharactersCog)
    cog.bot = SimpleNamespace(api=api)
    return cog


def _make_interaction(api: object) -> Mock:
    interaction = Mock(spec=discord.Interaction)
    interaction.response = Mock()
    interaction.response.defer = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.followup = Mock()
    interaction.followup.send = AsyncMock()
    interaction.client = SimpleNamespace(api=api)
    return interaction


class TestClassColor:
    def test_known_class_matches_wow_color(self) -> None:
        assert class_color("mage") == discord.Colour(0x69CCF0)

    def test_unknown_class_falls_back_to_slate(self) -> None:
        assert class_color("demonhunter") == Colors.SLATE

    def test_no_class_falls_back_to_slate(self) -> None:
        assert class_color(None) == Colors.SLATE


class TestResolveSlug:
    def test_name_and_surname_becomes_lowercase_slug(self) -> None:
        assert resolve_slug("Nox Zul") == "nox-zul"

    def test_extra_whitespace_is_stripped(self) -> None:
        assert resolve_slug("  Nox   ") is None  # single "word" after strip, no hyphen

    def test_existing_slug_is_lowercased(self) -> None:
        assert resolve_slug("Nox-Zul") == "nox-zul"

    def test_single_word_is_invalid(self) -> None:
        assert resolve_slug("Nox") is None

    def test_three_words_is_invalid(self) -> None:
        assert resolve_slug("Nox Van Zul") is None

    def test_empty_is_invalid(self) -> None:
        assert resolve_slug("   ") is None


class TestCharacterChoice:
    def test_formats_name_level_and_class(self) -> None:
        choice = character_choice(_summary(name="Nox", surname="Zul", level=60, **{"class": "mage"}))
        assert choice.name == "Nox Zul · Level 60 mage"
        assert choice.value == "nox-zul"

    def test_omits_class_when_unknown(self) -> None:
        choice = character_choice(_summary(**{"class": None}))
        assert choice.name == "Nox Zul · Level 60"


class TestBuildDescription:
    def test_full_summary(self) -> None:
        assert build_description(_summary()) == "Level 60 Gnome Mage · Frost"

    def test_no_profile_yet(self) -> None:
        assert (
            build_description(_summary(profile_found=False))
            == "Battle.net hasn't published this character's profile yet."
        )

    def test_missing_spec_and_race(self) -> None:
        character = _summary(race=None, spec=None, **{"class": None})
        assert build_description(character) == "Level 60"


class TestGuildValue:
    def test_in_roster_shows_rank(self) -> None:
        assert guild_value(_summary()) == "SideQuest (Guild Master)"

    def test_not_in_roster_omits_rank(self) -> None:
        assert guild_value(_summary(in_roster=False, rank_name=None)) == "SideQuest"

    def test_no_guild(self) -> None:
        assert guild_value(_summary(guild_name=None)) == "No guild"


class TestPvpValue:
    def test_both_known(self) -> None:
        assert pvp_value(_summary()) == "10 honorable kills · PvP rank 3"

    def test_missing_values_show_dash(self) -> None:
        assert pvp_value(_summary(honorable_kills=None, pvp_rank=None)) == "— honorable kills · PvP rank —"


class TestGearField:
    def test_gear_line_includes_item_level(self) -> None:
        assert gear_line(_gear()) == "Head: Lionheart Helm (65)"

    def test_gear_line_without_item_level(self) -> None:
        assert gear_line(_gear(item_level=None)) == "Head: Lionheart Helm"

    def test_ranks_by_item_level_and_caps_at_eight(self) -> None:
        gear = [_gear(slot_name=f"Slot{i}", name=f"Item{i}", item_level=i) for i in range(10)]
        field = build_gear_field(gear)
        lines = field.splitlines()
        assert len(lines) == 8
        assert lines[0] == "Slot9: Item9 (9)"
        assert lines[-1] == "Slot2: Item2 (2)"


class TestBuildCharacterEmbed:
    def test_title_url_and_color(self) -> None:
        embed = build_character_embed(_page())
        assert embed.title == "Nox Zul"
        assert embed.url == "https://sidequest-guild.com/characters/detail/?slug=nox-zul"
        assert embed.color == discord.Colour(0x69CCF0)
        assert embed.description == "Level 60 Gnome Mage · Frost"

    def test_includes_optional_fields_when_present(self) -> None:
        embed = build_character_embed(_page())
        field_names = [field.name for field in embed.fields]
        assert "Guild" in field_names
        assert "Item level" in field_names
        assert "Last login" in field_names
        assert "PvP" in field_names
        assert "Armory" in field_names

    def test_omits_missing_optional_fields(self) -> None:
        character = _page(item_level=None, last_login_at=None, honorable_kills=None, pvp_rank=None)
        embed = build_character_embed(character)
        field_names = [field.name for field in embed.fields]
        assert "Item level" not in field_names
        assert "Last login" not in field_names
        assert "PvP" not in field_names

    def test_no_profile_shows_no_gear_field(self) -> None:
        character = _page(profile_found=False, sheet=None)
        embed = build_character_embed(character)
        assert "Gear" not in [field.name for field in embed.fields]
        assert embed.description == "Battle.net hasn't published this character's profile yet."

    def test_gear_field_present_when_sheet_has_gear(self) -> None:
        character = _page(sheet=CharacterSheet(gear=[_gear()]))
        embed = build_character_embed(character)
        assert "Gear" in [field.name for field in embed.fields]


class TestAutocomplete:
    async def test_empty_text_uses_roster(self) -> None:
        api = AsyncMock()
        api.search_characters.return_value = [_summary()]
        interaction = _make_interaction(api)

        choices = await _autocomplete_character(interaction, "")

        api.search_characters.assert_awaited_once_with(None)
        assert choices[0].value == "nox-zul"

    async def test_one_character_returns_no_choices(self) -> None:
        api = AsyncMock()
        interaction = _make_interaction(api)

        choices = await _autocomplete_character(interaction, "n")

        api.search_characters.assert_not_awaited()
        assert choices == []

    async def test_two_or_more_characters_searches(self) -> None:
        api = AsyncMock()
        api.search_characters.return_value = [_summary()]
        interaction = _make_interaction(api)

        choices = await _autocomplete_character(interaction, "no")

        api.search_characters.assert_awaited_once_with("no")
        assert len(choices) == 1

    async def test_api_error_returns_empty_list(self) -> None:
        api = AsyncMock()
        api.search_characters.side_effect = APIError(500, "boom")
        interaction = _make_interaction(api)

        assert await _autocomplete_character(interaction, "no") == []


class TestWhoCommand:
    async def test_invalid_input_sends_hint_ephemeral(self) -> None:
        api = AsyncMock()
        cog = _make_cog(api)
        interaction = _make_interaction(api)

        await CharactersCog.who_command.callback(cog, interaction, "NoxZul")

        interaction.response.send_message.assert_awaited_once_with(NAME_HINT, ephemeral=True)
        api.get_character.assert_not_awaited()

    async def test_success_sends_public_embed(self) -> None:
        api = AsyncMock()
        api.get_character.return_value = _page()
        cog = _make_cog(api)
        interaction = _make_interaction(api)

        await CharactersCog.who_command.callback(cog, interaction, "Nox Zul")

        api.get_character.assert_awaited_once_with("nox-zul")
        interaction.followup.send.assert_awaited_once()
        kwargs = interaction.followup.send.await_args.kwargs
        assert kwargs["embed"].title == "Nox Zul"
        assert "ephemeral" not in kwargs

    async def test_not_found_sends_api_detail_ephemeral(self) -> None:
        api = AsyncMock()
        api.get_character.side_effect = NotFoundError(404, "Battle.net has no character x-y.")
        cog = _make_cog(api)
        interaction = _make_interaction(api)

        await CharactersCog.who_command.callback(cog, interaction, "Nox Zul")

        interaction.followup.send.assert_awaited_once_with("Battle.net has no character x-y.", ephemeral=True)

    async def test_unavailable_sends_clear_message(self) -> None:
        api = AsyncMock()
        api.get_character.side_effect = APIError(503, "Battle.net unavailable")
        cog = _make_cog(api)
        interaction = _make_interaction(api)

        await CharactersCog.who_command.callback(cog, interaction, "Nox Zul")

        interaction.followup.send.assert_awaited_once_with(UNAVAILABLE_MESSAGE, ephemeral=True)

    async def test_generic_error_reports_try_again(self) -> None:
        api = AsyncMock()
        api.get_character.side_effect = APIError(500, "boom")
        cog = _make_cog(api)
        interaction = _make_interaction(api)

        await CharactersCog.who_command.callback(cog, interaction, "Nox Zul")

        message = interaction.followup.send.await_args.args[0]
        assert "try again shortly" in message
