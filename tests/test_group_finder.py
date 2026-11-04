"""Unit tests for the group-finder plugin's pure logic: allocation, slugs, presence, teardown, succession, text."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from sidequest_bot.plugins.group_finder import (
    allocate_group_number,
    build_card_text,
    build_open_voice_custom_id,
    build_recap_line,
    extract_taken_numbers,
    format_duration,
    mapped_group_kind,
    named_channel_name,
    next_owner,
    pair_key,
    rename_cooldown_minutes,
    should_tear_down,
    slugify,
    tick_presence,
)
from sidequest_common.schemas import EventKind, SignupRole


class TestAllocateGroupNumber:
    def test_starts_at_one_when_nothing_taken(self) -> None:
        assert allocate_group_number([]) == 1

    def test_skips_taken_numbers(self) -> None:
        assert allocate_group_number([1, 2, 4]) == 3

    def test_fills_a_gap_left_by_a_torn_down_channel(self) -> None:
        assert allocate_group_number([1, 3]) == 2


class TestExtractTakenNumbers:
    def test_finds_default_named_channels(self) -> None:
        assert extract_taken_numbers(["dungeon-1", "dungeon-2", "raid-1"], "dungeon") == {1, 2}

    def test_ignores_renamed_channels(self) -> None:
        assert extract_taken_numbers(["dungeon-deadmines", "dungeon-1"], "dungeon") == {1}

    def test_ignores_other_prefixes(self) -> None:
        assert extract_taken_numbers(["raid-1", "raid-2"], "dungeon") == set()

    def test_empty_category_has_no_taken_numbers(self) -> None:
        assert extract_taken_numbers([], "dungeon") == set()


class TestSlugify:
    def test_lowercases_and_hyphenates(self) -> None:
        assert slugify("Deadmines") == "deadmines"
        assert slugify("The Deadmines Run") == "the-deadmines-run"

    def test_strips_non_ascii_alphanumerics(self) -> None:
        assert slugify("Zul'Farrak!!") == "zul-farrak"

    def test_truncates_to_max_length(self) -> None:
        assert len(slugify("a" * 100, max_length=10)) == 10

    def test_empty_when_nothing_alphanumeric_survives(self) -> None:
        assert slugify("!!!") == ""


class TestNamedChannelName:
    def test_uses_prefix_and_slug(self) -> None:
        assert named_channel_name(EventKind.DUNGEON, "deadmines", set()) == "dungeon-deadmines"

    def test_falls_back_to_bare_prefix_when_slug_is_empty(self) -> None:
        assert named_channel_name(EventKind.RAID, "", set()) == "raid"

    def test_deduplicates_with_a_numeric_suffix(self) -> None:
        existing = {"dungeon-deadmines"}
        assert named_channel_name(EventKind.DUNGEON, "deadmines", existing) == "dungeon-deadmines-2"

    def test_keeps_incrementing_past_a_taken_suffix(self) -> None:
        existing = {"dungeon-deadmines", "dungeon-deadmines-2"}
        assert named_channel_name(EventKind.DUNGEON, "deadmines", existing) == "dungeon-deadmines-3"


class TestPairKey:
    def test_orders_the_smaller_id_first(self) -> None:
        assert pair_key(5, 1) == (1, 5)
        assert pair_key(1, 5) == (1, 5)


class TestTickPresence:
    def test_adds_a_minute_to_everyone_present(self) -> None:
        minutes, _pairs, _peak, _grouped = tick_presence(
            present_ids=[1, 2], minutes={1: 10}, pairs={}, peak_size=2, grouped_minutes=5
        )
        assert minutes == {1: 11, 2: 1}

    def test_adds_a_minute_to_every_present_pair(self) -> None:
        _minutes, pairs, _peak, _grouped = tick_presence(
            present_ids=[3, 1, 2], minutes={}, pairs={}, peak_size=0, grouped_minutes=0
        )
        assert pairs == {(1, 2): 1, (1, 3): 1, (2, 3): 1}

    def test_peak_size_only_rises(self) -> None:
        _minutes, _pairs, peak, _grouped = tick_presence(
            present_ids=[1], minutes={}, pairs={}, peak_size=5, grouped_minutes=0
        )
        assert peak == 5

    def test_peak_size_rises_to_match_a_bigger_crowd(self) -> None:
        _minutes, _pairs, peak, _grouped = tick_presence(
            present_ids=[1, 2, 3], minutes={}, pairs={}, peak_size=1, grouped_minutes=0
        )
        assert peak == 3

    def test_grouped_minutes_only_rise_with_two_or_more_present(self) -> None:
        _minutes, _pairs, _peak, grouped_solo = tick_presence(
            present_ids=[1], minutes={}, pairs={}, peak_size=0, grouped_minutes=0
        )
        _minutes, _pairs, _peak, grouped_pair = tick_presence(
            present_ids=[1, 2], minutes={}, pairs={}, peak_size=0, grouped_minutes=0
        )
        assert grouped_solo == 0
        assert grouped_pair == 1

    def test_empty_presence_changes_nothing(self) -> None:
        minutes, pairs, peak, grouped = tick_presence(
            present_ids=[], minutes={1: 5}, pairs={(1, 2): 3}, peak_size=2, grouped_minutes=4
        )
        assert (minutes, pairs, peak, grouped) == ({1: 5}, {(1, 2): 3}, 2, 4)


class TestShouldTearDown:
    def test_none_never_tears_down(self) -> None:
        assert should_tear_down(None, datetime.now(UTC)) is False

    def test_under_the_grace_period_does_not_tear_down(self) -> None:
        now = datetime.now(UTC)
        assert should_tear_down(now - timedelta(seconds=60), now) is False

    def test_at_or_past_the_grace_period_tears_down(self) -> None:
        now = datetime.now(UTC)
        assert should_tear_down(now - timedelta(seconds=120), now) is True
        assert should_tear_down(now - timedelta(seconds=200), now) is True


class TestNextOwner:
    def test_no_candidates_returns_none(self) -> None:
        assert next_owner(1, [1], {}) is None

    def test_picks_the_longest_present_other_member(self) -> None:
        now = datetime.now(UTC)
        join_order = {1: now, 2: now - timedelta(minutes=10), 3: now - timedelta(minutes=5)}
        assert next_owner(1, [1, 2, 3], join_order) == 2

    def test_unknown_join_time_sorts_last(self) -> None:
        now = datetime.now(UTC)
        join_order = {2: now}
        assert next_owner(1, [1, 2, 3], join_order) == 2


class TestRenameCooldownMinutes:
    def test_no_renames_yet_allows_one_now(self) -> None:
        assert rename_cooldown_minutes([], datetime.now(UTC)) == 0

    def test_under_the_limit_allows_another(self) -> None:
        now = datetime.now(UTC)
        assert rename_cooldown_minutes([now - timedelta(minutes=1)], now) == 0

    def test_at_the_limit_requires_a_wait(self) -> None:
        now = datetime.now(UTC)
        recent = [now - timedelta(minutes=8), now - timedelta(minutes=2)]
        cooldown = rename_cooldown_minutes(recent, now)
        assert cooldown > 0

    def test_old_renames_outside_the_window_do_not_count(self) -> None:
        now = datetime.now(UTC)
        old = [now - timedelta(minutes=30), now - timedelta(minutes=20)]
        assert rename_cooldown_minutes(old, now) == 0


class TestBuildCardText:
    def test_shows_count_against_cap(self) -> None:
        text = build_card_text(
            display_name="dungeon-deadmines",
            member_count=3,
            cap=5,
            needs=[],
            note=None,
            owner_mention="<@1>",
            join_url="https://discord.com/x",
        )
        assert "3/5" in text
        assert "dungeon-deadmines" in text

    def test_shows_count_alone_without_a_cap(self) -> None:
        text = build_card_text(
            display_name="raid-1", member_count=10, cap=None, needs=[], note=None, owner_mention="<@1>", join_url="u"
        )
        assert "10" in text
        assert "/" not in text.split("·")[1]

    def test_includes_needed_roles(self) -> None:
        text = build_card_text(
            display_name="raid-1",
            member_count=1,
            cap=None,
            needs=[SignupRole.HEALER, SignupRole.TANK],
            note=None,
            owner_mention="<@1>",
            join_url="u",
        )
        assert "needs" in text
        assert "Healer" in text
        assert "Tank" in text

    def test_includes_note_when_set(self) -> None:
        text = build_card_text(
            display_name="raid-1",
            member_count=1,
            cap=None,
            needs=[],
            note="clearing fast",
            owner_mention="<@1>",
            join_url="u",
        )
        assert "clearing fast" in text

    def test_ends_with_a_join_link(self) -> None:
        text = build_card_text(
            display_name="raid-1", member_count=1, cap=None, needs=[], note=None, owner_mention="<@1>", join_url="u"
        )
        assert text.endswith("[Join](u)")


class TestFormatDuration:
    def test_under_an_hour_shows_minutes(self) -> None:
        assert format_duration(45) == "45m"

    def test_exact_hour_shows_no_minutes(self) -> None:
        assert format_duration(120) == "2h"

    def test_hour_and_minutes(self) -> None:
        assert format_duration(90) == "1h30m"


class TestBuildRecapLine:
    def test_includes_name_duration_and_participants(self) -> None:
        line = build_recap_line(
            display_name="dungeon-deadmines", duration_minutes=52, participant_names=["Alice", "Bob"]
        )
        assert line.startswith("🗡 dungeon-deadmines · 52m · ")
        assert "Alice, Bob" in line

    def test_no_participants_says_nobody(self) -> None:
        line = build_recap_line(display_name="raid-1", duration_minutes=1, participant_names=[])
        assert "nobody" in line


class TestMappedGroupKind:
    @pytest.mark.parametrize(
        "kind",
        [EventKind.DUNGEON, EventKind.RAID, EventKind.WORLD_BOSS, EventKind.LEVELING, EventKind.PVP, EventKind.SOCIAL],
    )
    def test_group_kinds_pass_through(self, kind: EventKind) -> None:
        assert mapped_group_kind(kind) == kind

    @pytest.mark.parametrize("kind", [EventKind.TRANSMOG, EventKind.OTHER])
    def test_other_kinds_fall_back_to_social(self, kind: EventKind) -> None:
        assert mapped_group_kind(kind) == EventKind.SOCIAL


class TestBuildOpenVoiceCustomId:
    def test_fits_discords_custom_id_limit(self) -> None:
        """No title or kind is encoded: a UUID alone leaves room, and never risks the whole card/thread send failing."""
        custom_id = build_open_voice_custom_id(uuid4())
        assert len(custom_id) <= 100

    def test_carries_only_the_event_id(self) -> None:
        event_id = uuid4()
        assert build_open_voice_custom_id(event_id) == f"sq:lfg:event:{event_id}"
