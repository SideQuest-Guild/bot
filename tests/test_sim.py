"""Unit tests for the sim plugin: the forum-post parser and the results table formatter."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import discord

from sidequest_bot.api_client import APIError, SimChange, SimComparison, SimItem, SimJob, SimMetrics, SimResult, SimRow
from sidequest_bot.plugins.sim import (
    FORUM_NAME,
    POST_NAME,
    USAGE,
    ParsedSim,
    SimCog,
    build_footer,
    build_request,
    format_sim_results,
    parse_item_line,
    parse_sim_post,
)

# --- parse_item_line


class TestParseItemLine:
    def test_plain_name(self) -> None:
        item = parse_item_line("Thunderfury, Blessed Blade of the Windseeker")
        assert isinstance(item, SimItem)
        assert item.item == "Thunderfury, Blessed Blade of the Windseeker"
        assert item.slot is None
        assert item.enchant is None

    def test_numeric_id(self) -> None:
        item = parse_item_line("19019")
        assert isinstance(item, SimItem)
        assert item.item == "19019"

    def test_wowhead_url_passed_through(self) -> None:
        item = parse_item_line("https://www.wowhead.com/forever/item=19019")
        assert isinstance(item, SimItem)
        assert item.item == "https://www.wowhead.com/forever/item=19019"

    def test_slot_prefix(self) -> None:
        item = parse_item_line("finger2: Seal of Rivendare")
        assert isinstance(item, SimItem)
        assert item.slot == "finger2"
        assert item.item == "Seal of Rivendare"

    def test_slot_prefix_case_insensitive(self) -> None:
        item = parse_item_line("Trinket1: Badge of Justice")
        assert isinstance(item, SimItem)
        assert item.slot == "trinket1"

    def test_enchant_suffix(self) -> None:
        item = parse_item_line("Breastplate of Valor +Agility")
        assert isinstance(item, SimItem)
        assert item.item == "Breastplate of Valor"
        assert item.enchant == "Agility"

    def test_slot_and_enchant(self) -> None:
        item = parse_item_line("chest: Breastplate of Valor +Agility")
        assert isinstance(item, SimItem)
        assert item.slot == "chest"
        assert item.item == "Breastplate of Valor"
        assert item.enchant == "Agility"

    def test_empty_after_slot_is_an_error(self) -> None:
        assert isinstance(parse_item_line("chest:"), str)


# --- parse_sim_post


class TestParseSimPost:
    def test_minimal_post(self) -> None:
        parsed = parse_sim_post("Nox Zul\nSeal of Rivendare", default_character=None)
        assert isinstance(parsed, ParsedSim)
        assert parsed.character == "Nox Zul"
        assert [item.item for item in parsed.swaps] == ["Seal of Rivendare"]
        assert parsed.wearing == []
        assert parsed.spec is None
        assert parsed.talents is None
        assert parsed.targets == [1, 4]
        assert parsed.mobs == ["humanoid"]
        assert parsed.duration == 90

    def test_multiple_items_and_options(self) -> None:
        text = (
            "Nox Zul\n"
            "https://www.wowhead.com/forever/item=19019\n"
            "finger2: Seal of Rivendare\n"
            "chest: Breastplate of Valor +Agility\n"
            "targets: 1, 4, 5\n"
            "mobs: humanoid, undead\n"
            "duration: 120\n"
            "spec: feralbear\n"
            "talents: abc123\n"
            "wearing: trinket1: Badge of Justice"
        )
        parsed = parse_sim_post(text, default_character=None)
        assert isinstance(parsed, ParsedSim)
        assert len(parsed.swaps) == 3
        assert parsed.swaps[1].slot == "finger2"
        assert parsed.swaps[2].enchant == "Agility"
        assert parsed.targets == [1, 4, 5]
        assert parsed.mobs == ["humanoid", "undead"]
        assert parsed.duration == 120
        assert parsed.spec == "feralbear"
        assert parsed.talents == "abc123"
        assert len(parsed.wearing) == 1
        assert parsed.wearing[0].slot == "trinket1"
        assert parsed.wearing[0].item == "Badge of Justice"

    def test_empty_post_is_usage(self) -> None:
        assert parse_sim_post("", default_character=None) == USAGE
        assert parse_sim_post("   \n  ", default_character=None) == USAGE

    def test_no_character_and_no_default_asks_for_one(self) -> None:
        result = parse_sim_post("targets: 1, 4\nSeal of Rivendare", default_character=None)
        assert isinstance(result, str)
        assert "Name a character" in result

    def test_no_character_line_uses_default(self) -> None:
        parsed = parse_sim_post("targets: 1\nSeal of Rivendare", default_character="Nox Zul")
        assert isinstance(parsed, ParsedSim)
        assert parsed.character == "Nox Zul"
        assert parsed.targets == [1]

    def test_no_items_is_an_error(self) -> None:
        result = parse_sim_post("Nox Zul\ntargets: 1, 4", default_character=None)
        assert isinstance(result, str)
        assert "at least one item" in result

    def test_too_many_items(self) -> None:
        text = "Nox Zul\n" + "\n".join(f"item {i}" for i in range(7))
        result = parse_sim_post(text, default_character=None)
        assert isinstance(result, str)
        assert "Up to 6 items" in result

    def test_too_many_wearing_corrections(self) -> None:
        text = "Nox Zul\nSeal of Rivendare\n" + "\n".join(f"wearing: item {i}" for i in range(18))
        result = parse_sim_post(text, default_character=None)
        assert isinstance(result, str)
        assert "wearing" in result

    def test_bad_targets_is_an_error(self) -> None:
        result = parse_sim_post("Nox Zul\nSeal of Rivendare\ntargets: a, b", default_character=None)
        assert isinstance(result, str)
        assert "targets:" in result

    def test_bad_duration_is_an_error(self) -> None:
        result = parse_sim_post("Nox Zul\nSeal of Rivendare\nduration: soon", default_character=None)
        assert isinstance(result, str)
        assert "duration:" in result


def test_build_request_sets_requested_by() -> None:
    parsed = ParsedSim(
        character="Nox Zul",
        swaps=[SimItem(item="Seal of Rivendare")],
        wearing=[],
        spec=None,
        talents=None,
        targets=[1, 4],
        mobs=["humanoid"],
        duration=90,
    )
    request = build_request(parsed, requested_by=123)
    assert request.character == "Nox Zul"
    assert request.requested_by == "123"
    request_anon = build_request(parsed, requested_by=None)
    assert request_anon.requested_by is None


# --- formatter


def _row(*, targets: int = 1, mob: str = "humanoid", delta: float = 5.0, noise: list[str] | None = None) -> SimRow:
    swapped = 1000.0 * (1 + delta / 100)
    return SimRow(
        targets=targets,
        mob=mob,
        baseline=SimMetrics(dps=1000.0, tps=1000.0, dtps=1000.0, hps=1000.0),
        swapped=SimMetrics(dps=swapped, tps=swapped, dtps=swapped, hps=swapped),
        delta_pct=SimMetrics(dps=delta, tps=delta, dtps=delta, hps=delta),
        noise=noise or [],
    )


def _result(**overrides: object) -> SimResult:
    base: dict[str, object] = {
        "character": "Nox Zul",
        "class_name": "paladin",
        "spec": "protection",
        "role": "tank",
        "level": 60,
        "gear_fetched_at": datetime(2026, 11, 1, tzinfo=UTC),
        "iterations": 5000,
        "duration": 90,
        "comparisons": [
            SimComparison(
                label="All swaps",
                changes=[
                    SimChange(
                        slot="finger2",
                        from_item={"id": 1, "name": "Old Ring"},
                        to_item={"id": 2, "name": "Seal of Rivendare"},
                    )
                ],
                rows=[_row(targets=1, delta=5.0), _row(targets=4, delta=3.0, noise=["tps", "dtps"])],
            )
        ],
        "notes": [],
    }
    base.update(overrides)
    return SimResult.model_validate(base)


class TestFormatSimResults:
    def test_header_and_table(self) -> None:
        chunks = format_sim_results(_result())
        assert len(chunks) == 1
        text = chunks[0]
        assert "Nox Zul" in text
        assert (
            "[Old Ring](<https://www.wowhead.com/forever/item=1>) → "
            "[Seal of Rivendare](<https://www.wowhead.com/forever/item=2>)"
        ) in text
        assert "Option 1" not in text  # one comparison: no option headings
        table = text.split("```")[1]
        header, first, second = table.strip().splitlines()[:3]
        assert header.split() == ["Targets", "Mob", "Δ", "Threat", "Δ", "Dmg", "Taken"]
        assert first.split() == ["1", "humanoid", "+5.0%", "+5.0%"]
        assert second.split() == ["4", "humanoid", "+3.0%", "~", "+3.0%", "~"]
        # columns line up: each value starts where its header does
        assert first.index("+5.0%") == header.index("Δ Threat")

    def test_several_comparisons_get_option_headings(self) -> None:
        comparison = _result().comparisons[0]
        text = format_sim_results(_result(comparisons=[comparison, comparison]))[0]
        assert "**Option 1**" in text
        assert "**Option 2**" in text

    def test_tank_role_shows_threat_and_damage_taken_columns(self) -> None:
        text = format_sim_results(_result(role="tank"))[0]
        assert "Δ Threat" in text
        assert "Δ Dmg Taken" in text
        assert "Δ DPS" not in text

    def test_dps_role_shows_dps_column(self) -> None:
        text = format_sim_results(_result(role="dps"))[0]
        assert "Δ DPS" in text
        assert "Δ Threat" not in text

    def test_noise_marked_and_explained(self) -> None:
        text = format_sim_results(_result())[0]
        assert "~ = noise" in text
        clean = _result(comparisons=[SimComparison(label="x", changes=[], rows=[_row()])])
        assert "~ = noise" not in format_sim_results(clean)[0]

    def test_footer_has_iterations_duration_and_gear_date(self) -> None:
        footer = build_footer(_result())
        assert "5000 iterations" in footer
        assert "90s fights" in footer
        assert "2026-11-01" in footer

    def test_notes_appear_in_footer(self) -> None:
        footer = build_footer(_result(notes=["Skipped enchant: Crusader (not mapped)"]))
        assert "Skipped enchant: Crusader (not mapped)" in footer

    def test_splits_long_results_into_multiple_chunks(self) -> None:
        mobs = ("humanoid", "undead", "beast", "demon", "dragonkin", "elemental", "giant", "mechanical")
        many_rows = [_row(targets=t, mob=m) for t in range(1, 6) for m in mobs]
        comparisons = [
            SimComparison(
                label=f"Comparison {i}",
                changes=[SimChange(slot="chest", from_item=None, to_item={"id": i, "name": f"Item {i}"})],
                rows=many_rows,
            )
            for i in range(4)
        ]
        chunks = format_sim_results(_result(comparisons=comparisons))
        assert len(chunks) > 1
        assert all(len(chunk) <= 2000 for chunk in chunks)


# --- cog behavior


def _forum(name: str = FORUM_NAME, threads: list | None = None, archived: list | None = None) -> MagicMock:
    forum = MagicMock(spec=discord.ForumChannel)
    forum.name = name
    forum.threads = threads or []
    forum.create_thread = AsyncMock()

    async def _archived(**_kwargs: object):
        for thread in archived or []:
            yield thread

    forum.archived_threads = _archived
    return forum


def _post(parent: MagicMock, *, name: str = POST_NAME, archived: bool = False) -> MagicMock:
    post = MagicMock(spec=discord.Thread)
    post.id = 100
    post.name = name
    post.parent = parent
    post.archived = archived
    post.edit = AsyncMock()
    return post


def _message(
    channel: object, content: str, *, author_id: int = 7, bot: bool = False, message_id: int = 500
) -> MagicMock:
    message = MagicMock(spec=discord.Message)
    message.id = message_id
    message.channel = channel
    message.content = content
    message.author = SimpleNamespace(id=author_id, bot=bot)
    message.guild = SimpleNamespace(id=42)
    message.reply = AsyncMock()
    return message


def _cog(api: object, guilds: list | None = None) -> SimCog:
    cog = SimCog.__new__(SimCog)
    cog.bot = SimpleNamespace(api=api, guilds=guilds or [])
    cog._polls = set()
    return cog


async def test_messages_outside_the_sim_post_are_ignored() -> None:
    api = AsyncMock()
    cog = _cog(api)
    for channel in (_post(_forum(), name="Paladin Hall"), _post(_forum("events"))):
        message = _message(channel, "Nox Zul\nSeal of Rivendare")
        await SimCog.on_message(cog, message)
        message.reply.assert_not_called()
    api.create_sim_job.assert_not_called()


async def test_the_posts_own_first_message_and_bot_messages_are_ignored() -> None:
    cog = _cog(AsyncMock())
    post = _post(_forum())
    for message in (_message(post, USAGE, message_id=post.id), _message(post, "Nox Zul\nSeal of Rivendare", bot=True)):
        await SimCog.on_message(cog, message)
        message.reply.assert_not_called()


async def test_a_message_without_items_gets_the_usage() -> None:
    cog = _cog(AsyncMock())
    message = _message(_post(_forum()), "")
    await SimCog.on_message(cog, message)
    assert message.reply.await_args.args[0] == USAGE


async def test_a_message_is_queued_for_its_author_and_answered_with_results() -> None:
    job_id = uuid4()
    api = AsyncMock()
    api.create_sim_job.return_value = SimJob(id=job_id, status="queued", position=1, created_at=datetime.now(UTC))
    api.get_sim_job.return_value = SimJob(id=job_id, status="done", result=_result(), created_at=datetime.now(UTC))
    cog = _cog(api)
    reply = MagicMock()
    reply.edit = AsyncMock()
    message = _message(_post(_forum()), "Nox Zul\nSeal of Rivendare", author_id=7)
    message.reply.return_value = reply

    await SimCog.on_message(cog, message)
    for task in list(cog._polls):
        await task

    guild_id, request = api.create_sim_job.await_args.args
    assert guild_id == 42
    assert request.character == "Nox Zul"
    assert request.requested_by == "7"
    assert "Seal of Rivendare" in reply.edit.await_args.kwargs["content"]


async def test_sweep_creates_the_post_with_the_usage_when_missing() -> None:
    forum = _forum()
    cog = _cog(AsyncMock(), guilds=[SimpleNamespace(id=42, forums=[forum])])
    await SimCog._post_sweep.coro(cog)
    forum.create_thread.assert_awaited_once_with(name=POST_NAME, content=USAGE)


async def test_sweep_reopens_an_archived_post_instead_of_making_another() -> None:
    forum = _forum()
    archived = _post(forum, archived=True)
    forum.archived_threads = _forum(archived=[archived]).archived_threads
    cog = _cog(AsyncMock(), guilds=[SimpleNamespace(id=42, forums=[forum])])
    await SimCog._post_sweep.coro(cog)
    forum.create_thread.assert_not_called()
    archived.edit.assert_awaited_once_with(archived=False)


def _request() -> object:
    return build_request(
        ParsedSim(
            character="Nox Zul",
            swaps=[SimItem(item="Seal of Rivendare")],
            wearing=[],
            spec=None,
            talents=None,
            targets=[1, 4],
            mobs=["humanoid"],
            duration=90,
        ),
        requested_by=1,
    )


async def test_poll_reports_a_queue_error() -> None:
    api = AsyncMock()
    api.create_sim_job.side_effect = APIError(400, "Unknown character 'Nox Zul'.")
    cog = _cog(api)
    reply = MagicMock()
    reply.edit = AsyncMock()
    await cog._poll(_message(_post(_forum()), ""), reply, _request())
    reply.edit.assert_awaited_once_with(content="Couldn't queue that sim: Unknown character 'Nox Zul'.")


async def test_poll_reports_a_failed_sim() -> None:
    api = AsyncMock()
    api.create_sim_job.return_value = SimJob(
        id=uuid4(), status="failed", error="wowsimcli exited 1", created_at=datetime.now(UTC)
    )
    cog = _cog(api)
    reply = MagicMock()
    reply.edit = AsyncMock()
    await cog._poll(_message(_post(_forum()), ""), reply, _request())
    reply.edit.assert_awaited_once_with(content="That sim failed: wowsimcli exited 1")
