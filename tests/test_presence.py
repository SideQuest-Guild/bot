"""The bot's rotating status line."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import discord

from sidequest_bot.plugins.presence import build_rotation, launch_activity
from sidequest_common.schemas import EventCounts, EventSummary

# 2026-10-28 12:00 Central
NOON_OCT_28 = datetime(2026, 10, 28, 17, 0, tzinfo=UTC)


def _event(
    title: str, starts_at: datetime, *, status: str = "scheduled", ends_at: datetime | None = None
) -> EventSummary:
    return EventSummary(
        id=uuid4(),
        kind="raid",
        title=title,
        location="",
        starts_at=starts_at,
        ends_at=ends_at,
        size=None,
        status=status,
        notes=None,
        counts=EventCounts(tank=0, healer=0, dps=0, signed=0, tentative=0, absent=0, interested=0),
        series=None,
    )


def _names(rotation: list[discord.BaseActivity]) -> list[str | None]:
    return [getattr(a, "name", None) for a in rotation]


class TestLaunchActivity:
    def test_counts_days_in_central_time(self) -> None:
        assert launch_activity(NOON_OCT_28).name == "7 days until Azeroth opens"
        # 11 PM Central on Nov 2 is already Nov 3 in UTC, but still two days out for the guild.
        assert launch_activity(datetime(2026, 11, 3, 4, 0, tzinfo=UTC)).name == "2 days until Azeroth opens"

    def test_tomorrow_launch_day_then_playing(self) -> None:
        assert launch_activity(datetime(2026, 11, 3, 18, 0, tzinfo=UTC)).name == "Azeroth opens tomorrow"
        assert launch_activity(datetime(2026, 11, 4, 18, 0, tzinfo=UTC)).name == "Launch day! See you in Azeroth"
        after = launch_activity(datetime(2026, 11, 5, 18, 0, tzinfo=UTC))
        assert isinstance(after, discord.Game)
        assert after.name == "World of Warcraft: Forever"


class TestBuildRotation:
    def test_running_event_pins_competing(self) -> None:
        live = _event("Molten Core", NOON_OCT_28 - timedelta(hours=1))
        rotation = build_rotation(NOON_OCT_28, [live, _event("Onyxia", NOON_OCT_28 + timedelta(days=1))])
        assert len(rotation) == 1
        assert rotation[0].type is discord.ActivityType.competing
        assert rotation[0].name == "Molten Core"

    def test_event_without_end_counts_as_running_for_three_hours(self) -> None:
        started = _event("Deadmines", NOON_OCT_28 - timedelta(hours=3, minutes=1))
        assert "Competing" not in str(_names(build_rotation(NOON_OCT_28, [started])))
        assert len(build_rotation(NOON_OCT_28, [started])) > 1

    def test_next_event_line_uses_relative_time(self) -> None:
        names = _names(
            build_rotation(NOON_OCT_28, [_event("Molten Core", NOON_OCT_28 + timedelta(hours=3, minutes=5))])
        )
        assert names[:2] == ["7 days until Azeroth opens", "Next up: Molten Core, in 3h"]
        later = _names(build_rotation(NOON_OCT_28, [_event("Onyxia", NOON_OCT_28 + timedelta(days=2, hours=1))]))
        assert "Next up: Onyxia, in 2 days" in later

    def test_cancelled_events_are_ignored(self) -> None:
        cancelled = _event("Molten Core", NOON_OCT_28 - timedelta(minutes=30), status="cancelled")
        names = _names(build_rotation(NOON_OCT_28, [cancelled]))
        assert not any(name and "Molten Core" in name for name in names)

    def test_no_events_still_rotates_flavour(self) -> None:
        names = _names(build_rotation(NOON_OCT_28, []))
        assert names[0] == "7 days until Azeroth opens"
        assert "the Goldshire Inn bard" in names
