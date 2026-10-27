from types import SimpleNamespace

import discord
import pytest

from sidequest_bot.plugins.moderation import (
    format_duration,
    hierarchy_violation,
    parse_duration,
    resolve_target_member,
    resolve_violation,
)


class TestParseDuration:
    @pytest.mark.parametrize(
        ("text", "expected_seconds"),
        [
            ("10m", 600),
            ("1h30m", 5400),
            ("2d", 172800),
            ("30s", 30),
            ("1d2h3m4s", 93784),
            ("28d", 28 * 86400),
        ],
    )
    def test_valid_durations(self, text: str, expected_seconds: int) -> None:
        assert parse_duration(text).total_seconds() == expected_seconds

    @pytest.mark.parametrize("text", ["", "abc", "10", "-5m", "5x", "m10", "10 minutes"])
    def test_invalid_durations_raise(self, text: str) -> None:
        with pytest.raises(ValueError, match="Invalid duration"):
            parse_duration(text)

    def test_zero_duration_rejected(self) -> None:
        with pytest.raises(ValueError, match="greater than zero"):
            parse_duration("0s")

    def test_exceeding_max_rejected(self) -> None:
        with pytest.raises(ValueError, match="can't exceed 28 days"):
            parse_duration("29d")

    def test_exactly_max_is_allowed(self) -> None:
        assert parse_duration("28d").days == 28


class TestFormatDuration:
    def test_zero_is_0s(self) -> None:
        assert format_duration(0) == "0s"

    def test_compound(self) -> None:
        assert format_duration(5400) == "1h30m"

    def test_single_unit(self) -> None:
        assert format_duration(172800) == "2d"


class TestHierarchyViolation:
    def _base(self, **overrides: object) -> dict[str, object]:
        base = {
            "actor_id": 1,
            "actor_top_role_position": 5,
            "actor_is_owner": False,
            "target_id": 2,
            "target_top_role_position": 3,
            "target_is_owner": False,
            "bot_id": 999,
            "bot_top_role_position": 10,
        }
        base.update(overrides)
        return base

    def test_allowed_when_target_is_lower(self) -> None:
        assert hierarchy_violation(**self._base()) is None

    def test_refuses_self(self) -> None:
        assert hierarchy_violation(**self._base(target_id=1)) is not None

    def test_refuses_bot(self) -> None:
        assert hierarchy_violation(**self._base(target_id=999)) is not None

    def test_refuses_guild_owner(self) -> None:
        assert hierarchy_violation(**self._base(target_is_owner=True)) is not None

    def test_refuses_equal_role_to_actor(self) -> None:
        assert hierarchy_violation(**self._base(target_top_role_position=5)) is not None

    def test_refuses_higher_role_than_actor(self) -> None:
        assert hierarchy_violation(**self._base(target_top_role_position=6)) is not None

    def test_owner_actor_bypasses_role_check(self) -> None:
        result = hierarchy_violation(
            **self._base(actor_is_owner=True, actor_top_role_position=1, target_top_role_position=6)
        )
        assert result is None

    def test_owner_actor_still_blocked_by_bot_hierarchy(self) -> None:
        result = hierarchy_violation(
            **self._base(
                actor_is_owner=True, actor_top_role_position=1, target_top_role_position=10, bot_top_role_position=10
            )
        )
        assert result is not None

    def test_refuses_equal_role_to_bot(self) -> None:
        result = hierarchy_violation(**self._base(target_top_role_position=10, bot_top_role_position=10))
        assert result is not None

    def test_refuses_higher_role_than_bot(self) -> None:
        result = hierarchy_violation(**self._base(target_top_role_position=11, bot_top_role_position=10))
        assert result is not None


def _member(member_id: int, *, top_role_position: int, owner_id: int) -> SimpleNamespace:
    member = SimpleNamespace(id=member_id, top_role=SimpleNamespace(position=top_role_position))
    member.guild = SimpleNamespace(owner_id=owner_id)
    return member


class TestResolveViolation:
    def test_member_target_uses_role_positions(self) -> None:
        actor = _member(1, top_role_position=5, owner_id=100)
        target = _member(2, top_role_position=6, owner_id=100)
        bot_member = _member(999, top_role_position=10, owner_id=100)
        violation = resolve_violation(actor=actor, target_id=target.id, target_member=target, bot_member=bot_member)
        assert violation is not None

    def test_member_target_allowed(self) -> None:
        actor = _member(1, top_role_position=5, owner_id=100)
        target = _member(2, top_role_position=3, owner_id=100)
        bot_member = _member(999, top_role_position=10, owner_id=100)
        assert resolve_violation(actor=actor, target_id=target.id, target_member=target, bot_member=bot_member) is None

    def test_non_member_target_only_checks_self_and_bot(self) -> None:
        actor = _member(1, top_role_position=5, owner_id=100)
        bot_member = _member(999, top_role_position=10, owner_id=100)
        # Not a guild member: role hierarchy can't apply, only self/bot checks do.
        assert resolve_violation(actor=actor, target_id=12345, target_member=None, bot_member=bot_member) is None

    def test_non_member_target_still_refuses_self(self) -> None:
        actor = _member(1, top_role_position=5, owner_id=100)
        bot_member = _member(999, top_role_position=10, owner_id=100)
        assert resolve_violation(actor=actor, target_id=1, target_member=None, bot_member=bot_member) is not None


def _fake_not_found() -> discord.NotFound:
    response = SimpleNamespace(status=404, reason="Not Found")
    return discord.NotFound(response, "Unknown Member")


class _FakeGuild:
    def __init__(self, *, cached_member: object | None, fetched_member: object | None, raises_not_found: bool) -> None:
        self._cached_member = cached_member
        self._fetched_member = fetched_member
        self._raises_not_found = raises_not_found
        self.fetch_member_calls = 0

    def get_member(self, _user_id: int) -> object | None:
        return self._cached_member

    async def fetch_member(self, _user_id: int) -> object:
        self.fetch_member_calls += 1
        if self._raises_not_found:
            raise _fake_not_found()
        return self._fetched_member


class TestResolveTargetMember:
    async def test_returns_cached_member_without_fetching(self) -> None:
        cached = object()
        guild = _FakeGuild(cached_member=cached, fetched_member=None, raises_not_found=False)
        result = await resolve_target_member(guild, 123)  # type: ignore[arg-type]
        assert result is cached
        assert guild.fetch_member_calls == 0

    async def test_cache_miss_falls_back_to_fetch(self) -> None:
        fetched = object()
        guild = _FakeGuild(cached_member=None, fetched_member=fetched, raises_not_found=False)
        result = await resolve_target_member(guild, 123)  # type: ignore[arg-type]
        assert result is fetched
        assert guild.fetch_member_calls == 1

    async def test_confirmed_not_a_member_returns_none(self) -> None:
        guild = _FakeGuild(cached_member=None, fetched_member=None, raises_not_found=True)
        result = await resolve_target_member(guild, 123)  # type: ignore[arg-type]
        assert result is None
