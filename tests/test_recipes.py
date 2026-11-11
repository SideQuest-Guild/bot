"""Unit tests for the recipes plugin: formatting helpers, embed builders and ``/recipe``."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord

from sidequest_bot.api_client import APIError, GuildCrafters, NotFoundError
from sidequest_bot.plugins.recipes import (
    NO_RESULTS_MESSAGE,
    NOT_DEPLOYED_MESSAGE,
    UNAVAILABLE_MESSAGE,
    RecipesCog,
    build_partial_recipe_embed,
    build_reagents_field,
    build_recipe_crafters_field,
    build_recipe_embed,
    crafted_item_value,
    partial_crafter_line,
    reagent_line,
    recipe_crafter_line,
)
from sidequest_common.schemas import (
    GuildCrafterProfessionCount,
    GuildCrafterResult,
    GuildCrafterRow,
    RecipeCraftedItem,
    RecipeCrafter,
    RecipeDetail,
    RecipeReagent,
)

_UNSET = object()


def _reagent(*, item_id: int = 100, name: str = "Linen Cloth", quantity: int = 2) -> RecipeReagent:
    return RecipeReagent(item_id=item_id, name=name, quantity=quantity, quality=None)


def _crafted_item(*, item_id: int = 200, name: str = "Linen Bandage", quality: str | None = None) -> RecipeCraftedItem:
    return RecipeCraftedItem(item_id=item_id, name=name, quality=quality)


def _recipe_crafter(
    *, character: str = "nox-zul", member_id: int | None = 1, member_name: str | None = "Nox", skill: int = 150
) -> RecipeCrafter:
    return RecipeCrafter(character=character, member_id=member_id, member_name=member_name, skill=skill)


def _recipe(
    *,
    recipe_id: int = 1,
    name: str = "First Aid: Linen Bandage",
    description: str | None = "Binds a wound.",
    rank: int | None = 1,
    profession: str | None = "First Aid",
    icon_url: str | None = "https://render.worldofwarcraft.com/icons/56/inv_misc_bandage.jpg",
    crafted_item: object = _UNSET,
    crafted_quantity: str | None = None,
    reagents: list[RecipeReagent] | None = None,
    crafters: list[RecipeCrafter] | None = None,
) -> RecipeDetail:
    return RecipeDetail(
        recipe_id=recipe_id,
        name=name,
        description=description,
        rank=rank,
        profession=profession,
        icon_url=icon_url,
        crafted_item=_crafted_item() if crafted_item is _UNSET else crafted_item,
        crafted_quantity=crafted_quantity,
        reagents=reagents if reagents is not None else [_reagent()],
        crafters=crafters if crafters is not None else [_recipe_crafter()],
    )


def _row(
    *,
    character: str = "nox-zul",
    member_id: int | None = 1,
    member_name: str | None = "Nox",
    profession: str = "First Aid",
    skill: int = 150,
) -> GuildCrafterRow:
    return GuildCrafterRow(
        character=character, member_id=member_id, member_name=member_name, profession=profession, skill=skill
    )


def _result(
    *,
    recipe_id: int = 1,
    recipe_name: str = "First Aid: Linen Bandage",
    item_id: int | None = 200,
    battlenet_recipe_id: int | None = 55,
    crafters: list[GuildCrafterRow] | None = None,
) -> GuildCrafterResult:
    return GuildCrafterResult(
        recipe_id=recipe_id,
        recipe_name=recipe_name,
        item_id=item_id,
        battlenet_recipe_id=battlenet_recipe_id,
        crafters=crafters if crafters is not None else [_row()],
    )


def _crafters(
    *, results: list[GuildCrafterResult] | None = None, professions: list[GuildCrafterProfessionCount] | None = None
) -> GuildCrafters:
    return GuildCrafters(
        results=results if results is not None else [_result()],
        professions=professions
        if professions is not None
        else [GuildCrafterProfessionCount(profession="First Aid", crafters=1)],
    )


def _make_cog(*, api: object | None = None) -> RecipesCog:
    cog = RecipesCog.__new__(RecipesCog)
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


class TestReagentLine:
    def test_formats_name_and_quantity(self) -> None:
        assert reagent_line(_reagent(name="Linen Cloth", quantity=2)) == "Linen Cloth x2"


class TestBuildReagentsField:
    def test_none_returns_placeholder(self) -> None:
        assert build_reagents_field([]) == "None"

    def test_caps_at_ten(self) -> None:
        reagents = [_reagent(item_id=i, name=f"Reagent{i}") for i in range(12)]
        field = build_reagents_field(reagents)
        assert len(field.splitlines()) == 10


class TestCraftedItemValue:
    def test_no_crafted_item_shows_dash(self) -> None:
        assert crafted_item_value(_recipe(crafted_item=None)) == "—"

    def test_includes_crafted_quantity_text(self) -> None:
        recipe = _recipe(crafted_item=_crafted_item(name="Linen Bandage"), crafted_quantity="Makes 2-4")
        assert crafted_item_value(recipe) == "Linen Bandage (Makes 2-4)"

    def test_no_quantity_text(self) -> None:
        recipe = _recipe(crafted_item=_crafted_item(name="Linen Bandage"), crafted_quantity=None)
        assert crafted_item_value(recipe) == "Linen Bandage"


class TestRecipeCrafterLine:
    def test_formats_name_and_skill(self) -> None:
        assert recipe_crafter_line(_recipe_crafter(character="nox-zul", skill=150)) == "Nox Zul — skill 150"


class TestBuildRecipeCraftersField:
    def test_no_crafters_shows_placeholder(self) -> None:
        assert build_recipe_crafters_field([]) == "No guild crafters know this recipe yet."

    def test_caps_at_ten(self) -> None:
        crafters = [_recipe_crafter(character=f"char{i}-name", skill=i) for i in range(12)]
        field = build_recipe_crafters_field(crafters)
        assert len(field.splitlines()) == 10


class TestBuildRecipeEmbed:
    def test_sets_title_and_thumbnail(self) -> None:
        embed = build_recipe_embed(_recipe(name="First Aid: Linen Bandage"))
        assert embed.title == "First Aid: Linen Bandage"
        assert embed.thumbnail.url == "https://render.worldofwarcraft.com/icons/56/inv_misc_bandage.jpg"

    def test_includes_profession_crafts_reagents_and_crafters(self) -> None:
        embed = build_recipe_embed(_recipe())
        field_names = {field.name: field.value for field in embed.fields}
        assert field_names["Profession"] == "First Aid"
        assert "Linen Bandage" in field_names["Crafts"]
        assert "Linen Cloth x2" in field_names["Reagents"]
        assert "Nox Zul" in field_names["Guild crafters"]

    def test_omits_profession_when_unknown(self) -> None:
        embed = build_recipe_embed(_recipe(profession=None))
        assert "Profession" not in [field.name for field in embed.fields]


class TestPartialCrafterLine:
    def test_formats_name_profession_and_skill(self) -> None:
        assert partial_crafter_line(_row()) == "Nox Zul — First Aid 150"


class TestBuildPartialRecipeEmbed:
    def test_includes_note_and_crafters(self) -> None:
        embed = build_partial_recipe_embed(_result(recipe_name="First Aid: Linen Bandage"))
        assert embed.title == "First Aid: Linen Bandage"
        assert "wasn't reachable" in (embed.description or "")
        assert "Nox Zul" in embed.fields[0].value

    def test_no_crafters_shows_none(self) -> None:
        embed = build_partial_recipe_embed(_result(crafters=[]))
        assert embed.fields[0].value == "None"


class TestRecipeCommand:
    async def test_no_guild_does_nothing(self) -> None:
        cog = _make_cog()
        interaction = _make_interaction(guild_id=None)

        await RecipesCog.recipe_command.callback(cog, interaction, "bandage")

        interaction.response.defer.assert_not_awaited()

    async def test_not_deployed_reports_clear_message(self) -> None:
        api = AsyncMock()
        api.get_crafters.side_effect = NotFoundError(404, "not found")
        cog = _make_cog(api=api)
        interaction = _make_interaction(api=api)

        await RecipesCog.recipe_command.callback(cog, interaction, "bandage")

        interaction.followup.send.assert_awaited_once_with(NOT_DEPLOYED_MESSAGE, ephemeral=True)

    async def test_search_unavailable_reports_clear_message(self) -> None:
        api = AsyncMock()
        api.get_crafters.side_effect = APIError(503, "Battle.net unavailable")
        cog = _make_cog(api=api)
        interaction = _make_interaction(api=api)

        await RecipesCog.recipe_command.callback(cog, interaction, "bandage")

        interaction.followup.send.assert_awaited_once_with(UNAVAILABLE_MESSAGE, ephemeral=True)

    async def test_search_generic_error_reports_try_again(self) -> None:
        api = AsyncMock()
        api.get_crafters.side_effect = APIError(500, "boom")
        cog = _make_cog(api=api)
        interaction = _make_interaction(api=api)

        await RecipesCog.recipe_command.callback(cog, interaction, "bandage")

        message = interaction.followup.send.await_args.args[0]
        assert "try again shortly" in message

    async def test_no_results_reports_clear_message(self) -> None:
        api = AsyncMock()
        api.get_crafters.return_value = _crafters(results=[])
        cog = _make_cog(api=api)
        interaction = _make_interaction(api=api)

        await RecipesCog.recipe_command.callback(cog, interaction, "nothing")

        interaction.followup.send.assert_awaited_once_with(NO_RESULTS_MESSAGE, ephemeral=True)

    async def test_no_battlenet_id_sends_partial_embed(self) -> None:
        api = AsyncMock()
        api.get_crafters.return_value = _crafters(results=[_result(battlenet_recipe_id=None)])
        cog = _make_cog(api=api)
        interaction = _make_interaction(api=api)

        await RecipesCog.recipe_command.callback(cog, interaction, "bandage")

        api.get_recipe.assert_not_awaited()
        kwargs = interaction.followup.send.await_args.kwargs
        assert "wasn't reachable" in (kwargs["embed"].description or "")

    async def test_success_sends_full_embed(self) -> None:
        api = AsyncMock()
        api.get_crafters.return_value = _crafters(results=[_result(battlenet_recipe_id=55)])
        api.get_recipe.return_value = _recipe(name="First Aid: Linen Bandage")
        cog = _make_cog(api=api)
        interaction = _make_interaction(api=api)

        await RecipesCog.recipe_command.callback(cog, interaction, "bandage")

        api.get_recipe.assert_awaited_once_with(55)
        kwargs = interaction.followup.send.await_args.kwargs
        assert kwargs["embed"].title == "First Aid: Linen Bandage"
        assert "ephemeral" not in kwargs

    async def test_recipe_detail_not_found_falls_back_to_partial(self) -> None:
        api = AsyncMock()
        api.get_crafters.return_value = _crafters(results=[_result(battlenet_recipe_id=55, recipe_name="Bandage")])
        api.get_recipe.side_effect = NotFoundError(404, "gone")
        cog = _make_cog(api=api)
        interaction = _make_interaction(api=api)

        await RecipesCog.recipe_command.callback(cog, interaction, "bandage")

        kwargs = interaction.followup.send.await_args.kwargs
        assert kwargs["embed"].title == "Bandage"
        assert "wasn't reachable" in (kwargs["embed"].description or "")

    async def test_recipe_detail_unavailable_reports_clear_message(self) -> None:
        api = AsyncMock()
        api.get_crafters.return_value = _crafters(results=[_result(battlenet_recipe_id=55)])
        api.get_recipe.side_effect = APIError(503, "Battle.net unavailable")
        cog = _make_cog(api=api)
        interaction = _make_interaction(api=api)

        await RecipesCog.recipe_command.callback(cog, interaction, "bandage")

        interaction.followup.send.assert_awaited_once_with(UNAVAILABLE_MESSAGE, ephemeral=True)
