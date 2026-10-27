from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from sidequest_bot.plugins.automod import (
    RULE_INVITE,
    RULE_LINK,
    AutomodCog,
    FloodTracker,
    contains_invite,
    contains_link,
    detect_content_violation,
    is_flooding,
    is_mention_spam,
)


def _fake_config(**overrides: object) -> SimpleNamespace:
    base = {
        "automod_enabled": True,
        "automod_block_invites": False,
        "automod_block_links": False,
        "automod_max_mentions": 0,
        "automod_spam_messages": 0,
        "automod_spam_seconds": 10,
        "automod_exempt_role_ids": [],
        "automod_exempt_channel_ids": [],
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _fake_message(*, guild_id: int = 99, member: object, content: str = "hello") -> MagicMock:
    message = MagicMock(spec=discord.Message)
    message.guild = MagicMock(spec=discord.Guild, id=guild_id)
    message.author = member
    message.channel = MagicMock(id=5)
    message.content = content
    message.mentions = []
    message.role_mentions = []
    return message


def _fake_member() -> MagicMock:
    member = MagicMock(spec=discord.Member)
    member.id = 1
    member.guild_permissions = discord.Permissions(manage_messages=False)
    member.roles = []
    return member


class TestContainsInvite:
    def test_detects_discord_gg(self) -> None:
        assert contains_invite("join us at discord.gg/abc123") is True

    def test_detects_discord_com_invite(self) -> None:
        assert contains_invite("https://discord.com/invite/abc123") is True

    def test_detects_discordapp_com_invite(self) -> None:
        assert contains_invite("discordapp.com/invite/abc123") is True

    def test_plain_message_has_no_invite(self) -> None:
        assert contains_invite("hey, how's it going?") is False

    def test_unrelated_link_is_not_an_invite(self) -> None:
        assert contains_invite("check out https://example.com") is False


class TestContainsLink:
    def test_detects_http(self) -> None:
        assert contains_link("http://example.com") is True

    def test_detects_https(self) -> None:
        assert contains_link("visit https://example.com/page") is True

    def test_plain_message_has_no_link(self) -> None:
        assert contains_link("no links here") is False


class TestDetectContentViolation:
    def test_invite_detected_when_blocking_invites(self) -> None:
        rule = detect_content_violation("discord.gg/abc", block_invites=True, block_links=False)
        assert rule is RULE_INVITE

    def test_invite_ignored_when_not_blocking_invites(self) -> None:
        rule = detect_content_violation("discord.gg/abc", block_invites=False, block_links=False)
        assert rule is None

    def test_generic_link_detected_when_blocking_links(self) -> None:
        rule = detect_content_violation("https://example.com", block_invites=True, block_links=True)
        assert rule is RULE_LINK

    def test_invite_takes_priority_over_generic_link_rule(self) -> None:
        # An invite link also matches the generic link pattern; invites should win so it's reported correctly.
        rule = detect_content_violation("https://discord.gg/abc", block_invites=True, block_links=True)
        assert rule is RULE_INVITE

    def test_clean_message_has_no_violation(self) -> None:
        assert detect_content_violation("hello world", block_invites=True, block_links=True) is None


class TestIsMentionSpam:
    def test_below_threshold_is_fine(self) -> None:
        assert is_mention_spam([1, 2], [3], max_mentions=5) is False

    def test_above_threshold_is_spam(self) -> None:
        assert is_mention_spam([1, 2, 3], [4, 5, 6], max_mentions=5) is True

    def test_exactly_at_threshold_is_fine(self) -> None:
        assert is_mention_spam([1, 2, 3], [], max_mentions=3) is False

    def test_duplicate_mentions_count_once(self) -> None:
        # 1 appears in both user and role mentions (edge case), unique count is still 1.
        assert is_mention_spam([1], [1], max_mentions=1) is False

    def test_zero_threshold_disables_check(self) -> None:
        assert is_mention_spam([1, 2, 3, 4, 5, 6], [], max_mentions=0) is False


class TestFloodTracker:
    def test_first_hit_counts_one(self) -> None:
        clock = iter([0.0])
        tracker = FloodTracker(clock=lambda: next(clock))
        assert tracker.hit(guild_id=1, user_id=1, window_seconds=10) == 1

    def test_hits_within_window_accumulate(self) -> None:
        times = iter([0.0, 1.0, 2.0])
        tracker = FloodTracker(clock=lambda: next(times))
        tracker.hit(guild_id=1, user_id=1, window_seconds=10)
        tracker.hit(guild_id=1, user_id=1, window_seconds=10)
        assert tracker.hit(guild_id=1, user_id=1, window_seconds=10) == 3

    def test_hits_outside_window_expire(self) -> None:
        times = iter([0.0, 20.0])
        tracker = FloodTracker(clock=lambda: next(times))
        tracker.hit(guild_id=1, user_id=1, window_seconds=10)
        # 20s later, well outside the 10s window: the earlier hit should have expired.
        assert tracker.hit(guild_id=1, user_id=1, window_seconds=10) == 1

    def test_different_users_tracked_independently(self) -> None:
        tracker = FloodTracker(clock=lambda: 0.0)
        tracker.hit(guild_id=1, user_id=1, window_seconds=10)
        assert tracker.hit(guild_id=1, user_id=2, window_seconds=10) == 1

    def test_different_guilds_tracked_independently(self) -> None:
        tracker = FloodTracker(clock=lambda: 0.0)
        tracker.hit(guild_id=1, user_id=1, window_seconds=10)
        assert tracker.hit(guild_id=2, user_id=1, window_seconds=10) == 1


class TestFloodTrackerSweep:
    def test_sweep_evicts_idle_keys(self) -> None:
        times = iter([0.0, 100.0])
        tracker = FloodTracker(clock=lambda: next(times))
        tracker.hit(guild_id=1, user_id=1, window_seconds=10)
        tracker.sweep(max_idle_seconds=50)
        assert (1, 1) not in tracker._timestamps

    def test_sweep_keeps_recently_active_keys(self) -> None:
        times = iter([0.0, 10.0])
        tracker = FloodTracker(clock=lambda: next(times))
        tracker.hit(guild_id=1, user_id=1, window_seconds=10)
        tracker.sweep(max_idle_seconds=50)
        assert (1, 1) in tracker._timestamps

    def test_sweep_only_evicts_the_idle_key(self) -> None:
        times = iter([0.0, 0.0, 100.0])
        tracker = FloodTracker(clock=lambda: next(times))
        tracker.hit(guild_id=1, user_id=1, window_seconds=10)  # idle after this
        tracker.hit(guild_id=1, user_id=2, window_seconds=10)  # still active below
        # Give user 2 a fresh hit right before sweeping so it stays active.
        tracker._timestamps[(1, 2)] = [99.0]
        tracker.sweep(max_idle_seconds=50)
        assert (1, 1) not in tracker._timestamps
        assert (1, 2) in tracker._timestamps


class TestIsFlooding:
    def test_below_threshold_is_fine(self) -> None:
        assert is_flooding(3, max_messages=5) is False

    def test_above_threshold_is_flooding(self) -> None:
        assert is_flooding(6, max_messages=5) is True

    def test_exactly_at_threshold_is_fine(self) -> None:
        assert is_flooding(5, max_messages=5) is False

    def test_zero_threshold_disables_check(self) -> None:
        assert is_flooding(1000, max_messages=0) is False


class TestFloodTrackingSkipsWhenDisabled:
    async def test_hit_not_recorded_when_spam_messages_is_zero(self) -> None:
        cog = AutomodCog.__new__(AutomodCog)
        cog.bot = SimpleNamespace(configs=SimpleNamespace(get=AsyncMock(return_value=_fake_config())))
        cog.flood_tracker = MagicMock(spec=FloodTracker)

        await cog._moderate(_fake_message(member=_fake_member()))

        cog.flood_tracker.hit.assert_not_called()

    async def test_hit_recorded_when_spam_messages_enabled(self) -> None:
        cog = AutomodCog.__new__(AutomodCog)
        cog.bot = SimpleNamespace(
            configs=SimpleNamespace(get=AsyncMock(return_value=_fake_config(automod_spam_messages=5)))
        )
        cog.flood_tracker = MagicMock(spec=FloodTracker)
        cog.flood_tracker.hit.return_value = 1

        await cog._moderate(_fake_message(member=_fake_member()))

        cog.flood_tracker.hit.assert_called_once()
