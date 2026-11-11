"""Unit tests for the crafters plugin: formatting helpers, autocomplete and ``/crafter``."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord

from sidequest_bot.api_client import APIError, GuildCrafters, NotFoundError
from sidequest_bot.plugins.crafters import (
    NO_RESULTS_MESSAGE,
    NOT_DEPLOYED_MESSAGE,
    UNAVAILABLE_MESSAGE,
    CraftersCog,
    _autocomplete_profession,
    build_crafters_embed,
    build_result_field,
    character_label,
    crafter_line,
)
from sidequest_common.schemas import GuildCrafterProfessionCount, GuildCrafterResult, GuildCrafterRow


def _row(
    *,
    character: str = "nox-zul",
    member_id: int | None = 1,
    member_name: str | None = "Nox",
    profession: str = "Tailoring",
    skill: int = 300,
) -> GuildCrafterRow:
    return GuildCrafterRow(
        character=character, member_id=member_id, member_name=member_name, profession=profession, skill=skill
    )


def _result(
    *,
    recipe_id: int = 1,
    recipe_name: str = "Robe of Winter Night",
    item_id: int | None = 5000,
    crafters: list[GuildCrafterRow] | None = None,
) -> GuildCrafterResult:
    return GuildCrafterResult(
        recipe_id=recipe_id,
        recipe_name=recipe_name,
        item_id=item_id,
        battlenet_recipe_id=None,
        crafters=crafters if crafters is not None else [_row()],
    )


def _crafters(
    *, results: list[GuildCrafterResult] | None = None, professions: list[GuildCrafterProfessionCount] | None = None
) -> GuildCrafters:
    return GuildCrafters(
        results=results if results is not None else [_result()],
        professions=professions
        if professions is not None
        else [GuildCrafterProfessionCount(profession="Tailoring", crafters=1)],
    )


def _make_cog(*, api: object | None = None) -> CraftersCog:
    cog = CraftersCog.__new__(CraftersCog)
    cog.bot = SimpleNamespace(api=api or AsyncMock())
    return cog


def _make_interaction(*, api: object | None = None, guild_id: int | None = 1) -> Mock:
    interaction = Mock(spec=discord.Interaction)
    interaction.guild_id = guild_id
    interaction.response = Mock()
    interaction.response.defer = AsyncMock()
    interaction.followup = Mock()
    interaction.followup.send = AsyncMock()
    interaction.client = SimpleNamespace(api=api or AsyncMock())
    return interaction


class TestCharacterLabel:
    def test_title_cases_each_part(self) -> None:
        assert character_label("nox-zul") == "Nox Zul"


class TestCrafterLine:
    def test_formats_name_profession_and_skill(self) -> None:
        assert crafter_line(_row()) == "Nox Zul — Tailoring 300"


class TestBuildResultField:
    def test_caps_at_five_and_notes_extra(self) -> None:
        rows = [_row(character=f"char{i}-name", skill=i) for i in range(7)]
        field = build_result_field(_result(crafters=rows))
        lines = field.splitlines()
        assert len(lines) == 6
        assert lines[-1] == "…and 2 more"


class TestBuildCraftersEmbed:
    def test_one_field_per_recipe(self) -> None:
        embed = build_crafters_embed(_crafters(), query="robe", profession=None)
        assert embed.title == "Crafters"
        assert embed.fields[0].name == "Robe of Winter Night"
        assert "Nox Zul" in embed.fields[0].value

    def test_description_includes_query_and_profession(self) -> None:
        embed = build_crafters_embed(_crafters(), query="robe", profession="Tailoring")
        assert embed.description == '"robe" · Tailoring'

    def test_caps_results_and_adds_footer(self) -> None:
        results = [_result(recipe_id=i, recipe_name=f"Recipe{i}") for i in range(12)]
        embed = build_crafters_embed(_crafters(results=results), query=None, profession=None)
        assert len(embed.fields) == 10
        assert embed.footer.text == "12 recipes matched; showing the first 10."


class TestAutocompleteProfession:
    async def test_no_guild_returns_empty(self) -> None:
        interaction = _make_interaction(guild_id=None)
        assert await _autocomplete_profession(interaction, "tail") == []

    async def test_matches_profession_substring(self) -> None:
        api = AsyncMock()
        api.get_crafters.return_value = _crafters(
            professions=[
                GuildCrafterProfessionCount(profession="Tailoring", crafters=2),
                GuildCrafterProfessionCount(profession="Alchemy", crafters=1),
            ]
        )
        interaction = _make_interaction(api=api)

        choices = await _autocomplete_profession(interaction, "tail")

        assert [choice.value for choice in choices] == ["Tailoring"]

    async def test_api_error_returns_empty(self) -> None:
        api = AsyncMock()
        api.get_crafters.side_effect = APIError(503, "unavailable")
        interaction = _make_interaction(api=api)
        assert await _autocomplete_profession(interaction, "tail") == []


class TestCrafterCommand:
    async def test_not_deployed_reports_clear_message(self) -> None:
        api = AsyncMock()
        api.get_crafters.side_effect = NotFoundError(404, "not found")
        cog = _make_cog(api=api)
        interaction = _make_interaction(api=api)

        await CraftersCog.crafter_command.callback(cog, interaction, "robe", None)

        interaction.followup.send.assert_awaited_once_with(NOT_DEPLOYED_MESSAGE, ephemeral=True)

    async def test_api_error_reports_unavailable_message(self) -> None:
        api = AsyncMock()
        api.get_crafters.side_effect = APIError(503, "Battle.net unavailable")
        cog = _make_cog(api=api)
        interaction = _make_interaction(api=api)

        await CraftersCog.crafter_command.callback(cog, interaction, "robe", None)

        interaction.followup.send.assert_awaited_once_with(UNAVAILABLE_MESSAGE, ephemeral=True)

    async def test_no_results_reports_clear_message(self) -> None:
        api = AsyncMock()
        api.get_crafters.return_value = _crafters(results=[])
        cog = _make_cog(api=api)
        interaction = _make_interaction(api=api)

        await CraftersCog.crafter_command.callback(cog, interaction, "nothing", None)

        interaction.followup.send.assert_awaited_once_with(NO_RESULTS_MESSAGE, ephemeral=True)

    async def test_success_sends_public_embed(self) -> None:
        api = AsyncMock()
        api.get_crafters.return_value = _crafters()
        cog = _make_cog(api=api)
        interaction = _make_interaction(api=api)

        await CraftersCog.crafter_command.callback(cog, interaction, "robe", "Tailoring")

        api.get_crafters.assert_awaited_once_with(1, q="robe", profession="Tailoring")
        kwargs = interaction.followup.send.await_args.kwargs
        assert kwargs["embed"].title == "Crafters"
        assert "ephemeral" not in kwargs

    async def test_no_guild_does_nothing(self) -> None:
        cog = _make_cog()
        interaction = _make_interaction(guild_id=None)

        await CraftersCog.crafter_command.callback(cog, interaction, "robe", None)

        interaction.response.defer.assert_not_awaited()
