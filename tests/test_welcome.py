"""Unit tests for the welcome plugin's pure helpers: pending deferral, autorole safety, rendering."""

from unittest.mock import patch

from sidequest_bot.plugins.welcome import (
    autorole_block_reason,
    completed_screening,
    placeholder_help,
    render_welcome,
)
from sidequest_common.templates import TEMPLATE_PLACEHOLDERS


class TestCompletedScreening:
    def test_pending_to_not_pending_is_completed(self) -> None:
        assert completed_screening(before_pending=True, after_pending=False) is True

    def test_still_pending_is_not_completed(self) -> None:
        assert completed_screening(before_pending=True, after_pending=True) is False

    def test_never_pending_is_not_completed(self) -> None:
        # Normal join: never pending, so on_member_join (not the deferred update path) handles it.
        assert completed_screening(before_pending=False, after_pending=False) is False

    def test_becoming_pending_is_not_completed(self) -> None:
        # Shouldn't normally happen, but pending flipping the other way is not a completion.
        assert completed_screening(before_pending=False, after_pending=True) is False


class TestAutoroleBlockReason:
    """Both halves of the check are required: configuring (who) and granting (what)."""

    def test_allowed_when_both_checks_pass(self) -> None:
        with (
            patch("sidequest_bot.plugins.welcome.configure_block_reason", return_value=None) as configure,
            patch("sidequest_bot.plugins.welcome.grant_block_reason", return_value=None) as grant,
        ):
            assert autorole_block_reason("member", "role") is None
        configure.assert_called_once_with("member", "role")
        grant.assert_called_once_with("role")

    def test_blocked_when_member_cannot_configure_the_role(self) -> None:
        # Manage-Server member trying to set up a role above their own rank: the privilege-escalation case.
        with (
            patch(
                "sidequest_bot.plugins.welcome.configure_block_reason",
                return_value="That role is at or above your highest role.",
            ),
            patch("sidequest_bot.plugins.welcome.grant_block_reason", return_value=None) as grant,
        ):
            reason = autorole_block_reason("member", "role")
        assert reason == "That role is at or above your highest role."
        # Short-circuits: no need to ask whether the bot may grant it once the configurer is already blocked.
        grant.assert_not_called()

    def test_blocked_when_role_has_dangerous_permissions(self) -> None:
        # Member is allowed to configure it, but the role itself carries admin-tier permissions.
        with (
            patch("sidequest_bot.plugins.welcome.configure_block_reason", return_value=None),
            patch(
                "sidequest_bot.plugins.welcome.grant_block_reason",
                return_value="That role has administrator permissions, so I won't hand it out.",
            ),
        ):
            reason = autorole_block_reason("member", "role")
        assert reason == "That role has administrator permissions, so I won't hand it out."

    def test_configure_reason_wins_over_grant_reason(self) -> None:
        # If both are blocked, report the configure reason first (it's the actionable one for the caller).
        with (
            patch("sidequest_bot.plugins.welcome.configure_block_reason", return_value="configure blocked"),
            patch("sidequest_bot.plugins.welcome.grant_block_reason", return_value="grant blocked"),
        ):
            assert autorole_block_reason("member", "role") == "configure blocked"


class TestRenderWelcome:
    def test_fills_all_placeholders(self) -> None:
        text = render_welcome(
            "Welcome {mention} ({user}) to {guild}! We're now {member_count} strong.",
            mention="<@123>",
            user="Alice",
            guild="SideQuest Guild",
            member_count=42,
        )
        assert text == "Welcome <@123> (Alice) to SideQuest Guild! We're now 42 strong."

    def test_unknown_placeholder_left_untouched(self) -> None:
        text = render_welcome(
            "Welcome {mention}, level {level}!", mention="<@123>", user="Alice", guild="g", member_count=1
        )
        # "level" isn't a welcome placeholder; render_template leaves unknown placeholders as-is.
        assert text == "Welcome <@123>, level {level}!"


class TestPlaceholderHelp:
    def test_lists_every_known_placeholder(self) -> None:
        help_text = placeholder_help()
        for name in TEMPLATE_PLACEHOLDERS:
            assert f"{{{name}}}" in help_text
