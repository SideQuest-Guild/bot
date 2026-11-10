"""Unit tests for the group-finder plugin's pure logic: allocation, slugs, presence, teardown, succession, text."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from sidequest_bot.plugins.group_finder import (
    CardContent,
    SpawnButton,
    allocate_group_number,
    build_card_content,
    build_open_voice_custom_id,
    build_panel_layout,
    build_recap_line,
    extract_taken_numbers,
    format_duration,
    mapped_group_kind,
    named_channel_name,
    next_owner,
    pair_key,
    pug_channel_name,
    pug_sweep_decision,
    rename_cooldown_minutes,
    should_tear_down,
    slugify,
    tick_presence,
    tick_pug_minutes,
)
from sidequest_common.schemas import GROUP_KINDS, EventKind, SignupRole


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


class TestTickPugMinutes:
    def test_rises_when_a_counted_member_and_an_outsider_share_the_channel(self) -> None:
        assert tick_pug_minutes(5, counted_ids={1}, outsider_ids={2}) == 6

    def test_unchanged_with_no_counted_members(self) -> None:
        assert tick_pug_minutes(5, counted_ids=set(), outsider_ids={2}) == 5

    def test_unchanged_with_no_outsiders(self) -> None:
        assert tick_pug_minutes(5, counted_ids={1}, outsider_ids=set()) == 5

    def test_unchanged_with_nobody_present(self) -> None:
        assert tick_pug_minutes(5, counted_ids=set(), outsider_ids=set()) == 5


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


def _card(**overrides: object) -> CardContent:
    facts: dict[str, object] = {
        "kind": EventKind.DUNGEON,
        "display_name": "dungeon-deadmines",
        "member_ids": [11, 22, 33],
        "cap": 5,
        "needs": [],
        "note": None,
        "owner_id": 11,
        "locked": False,
        "started_at": datetime(2026, 11, 4, 20, 0, tzinfo=UTC),
    }
    return build_card_content(**{**facts, **overrides})  # type: ignore[arg-type]


class TestBuildCardContent:
    def test_counts_players_against_the_cap_with_a_slot_bar(self) -> None:
        card = _card()
        assert "**3/5** in voice" in card.header
        assert "▰▰▰▱▱" in card.header
        assert not card.full

    def test_an_uncapped_group_shows_only_the_count(self) -> None:
        card = _card(kind=EventKind.SOCIAL, cap=None)
        assert "**3** in voice" in card.header
        assert "▰" not in card.header

    def test_a_big_raid_gets_no_slot_bar(self) -> None:
        assert "▰" not in _card(kind=EventKind.RAID, cap=40).header

    def test_a_group_nobody_has_joined_says_so(self) -> None:
        assert "Waiting for the first player" in _card(member_ids=[]).header

    def test_a_group_at_its_cap_is_full(self) -> None:
        assert _card(member_ids=[1, 2, 3, 4, 5]).full

    def test_details_name_the_owner_needs_note_and_lock(self) -> None:
        card = _card(needs=[SignupRole.TANK, SignupRole.HEALER], note="need key holder", locked=True)
        assert "<@11>" in card.details
        assert "Tank" in card.details
        assert "Healer" in card.details
        assert "> need key holder" in card.details
        assert "Invite only" in card.details

    def test_lists_at_most_ten_members_then_a_count(self) -> None:
        card = _card(kind=EventKind.RAID, cap=40, owner_id=1, member_ids=list(range(1, 15)))
        assert "<@10>" in card.details
        assert "<@11>" not in card.details
        assert "+4 more" in card.details


class TestBuildPanelLayout:
    def test_offers_every_group_kind(self) -> None:
        layout = build_panel_layout()
        custom_ids = {
            getattr(item, "custom_id", None) for item in layout.walk_children() if isinstance(item, SpawnButton)
        }
        assert custom_ids == {f"sq:lfg:spawn:{kind.value}" for kind in GROUP_KINDS}


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


class TestPugChannelName:
    def test_marks_the_channel_as_pug_with_kind_and_number(self) -> None:
        assert pug_channel_name(EventKind.RAID, 1) == "pug-raid-1"
        assert pug_channel_name(EventKind.DUNGEON, 2) == "pug-dungeon-2"


class TestPugSweepDecision:
    _NOW = datetime(2026, 1, 1, tzinfo=UTC)

    def _decide(self, **overrides: object) -> str:
        base: dict[str, object] = {
            "member_role_ids": set(),
            "assignable_role_ids": set(),
            "linked_role_id": 10,
            "roster_role_id": 11,
            "pug_role_id": 1,
            "gate_role_id": 12,
            "group_pug_role_ids": set(),
            "orphaned_since": None,
            "now": self._NOW,
        }
        base.update(overrides)
        return pug_sweep_decision(**base)  # type: ignore[arg-type]

    def test_strips_once_signed_in(self) -> None:
        assert self._decide(member_role_ids={1, 10}) == "strip"

    def test_strips_once_roster_confirmed(self) -> None:
        assert self._decide(member_role_ids={1, 11}) == "strip"

    def test_confirmed_but_no_longer_holding_pug_role_is_a_no_op(self) -> None:
        assert self._decide(member_role_ids={10}) == "none"

    def test_still_in_a_live_group_is_never_orphaned(self) -> None:
        assert self._decide(member_role_ids={1, 20}, group_pug_role_ids={20}, orphaned_since=self._NOW) == "none"

    def test_no_orphan_clock_yet_is_none(self) -> None:
        assert self._decide(member_role_ids={1}, orphaned_since=None) == "none"

    def test_within_the_grace_window_is_none(self) -> None:
        since = self._NOW - timedelta(minutes=10)
        assert self._decide(member_role_ids={1}, orphaned_since=since) == "none"

    def test_kicked_once_orphaned_past_the_window_holding_only_allowed_roles(self) -> None:
        since = self._NOW - timedelta(minutes=31)
        assert (
            self._decide(member_role_ids={1}, assignable_role_ids={1, 12}, orphaned_since=since, now=self._NOW)
            == "kick"
        )

    def test_never_kicked_while_holding_any_other_role(self) -> None:
        since = self._NOW - timedelta(minutes=31)
        assert (
            self._decide(member_role_ids={1}, assignable_role_ids={1, 12, 999}, orphaned_since=since, now=self._NOW)
            == "none"
        )

    def test_kicked_at_exactly_the_orphan_window(self) -> None:
        since = self._NOW - timedelta(seconds=1800)
        assert (
            self._decide(member_role_ids={1}, assignable_role_ids={1, 12}, orphaned_since=since, now=self._NOW)
            == "kick"
        )

    def test_not_kicked_just_under_the_orphan_window(self) -> None:
        since = self._NOW - timedelta(seconds=1799)
        assert (
            self._decide(member_role_ids={1}, assignable_role_ids={1, 12}, orphaned_since=since, now=self._NOW)
            == "none"
        )
