import discord

from sidequest_bot.plugins.signup_notices import (
    GUEST_STATUS_TEXT,
    STATUS_FIELD_NAME,
    apply_approval_status,
    avatar_url,
    build_custom_id,
    build_notice_embed,
    build_notice_view,
    clicker_is_gm,
    more_info_url,
    parse_custom_id,
)
from sidequest_common.schemas import SignupNoticeCandidate


def _candidate(**overrides: object) -> SignupNoticeCandidate:
    base: dict[str, object] = {
        "member_id": 1,
        "discord_id": "123456789012345678",
        "display_name": "Jaina",
        "username": "jaina.proudmoore",
        "avatar_hash": None,
        "battletag": None,
        "roster_confirmed": False,
        "created_at": "2026-01-01T00:00:00Z",
    }
    base.update(overrides)
    return SignupNoticeCandidate.model_validate(base)


class TestCustomId:
    def test_round_trips(self) -> None:
        assert parse_custom_id(build_custom_id(42)) == 42

    def test_rejects_garbage(self) -> None:
        assert parse_custom_id("sq:rm:not-this") is None
        assert parse_custom_id("sq:sn:approve:not-a-number") is None


class TestAvatarUrl:
    def test_custom_avatar_uses_png_extension(self) -> None:
        assert avatar_url(1, "abc123") == "https://cdn.discordapp.com/avatars/1/abc123.png"

    def test_animated_avatar_uses_gif_extension(self) -> None:
        assert avatar_url(1, "a_abc123") == "https://cdn.discordapp.com/avatars/1/a_abc123.gif"

    def test_no_avatar_falls_back_to_default(self) -> None:
        url = avatar_url(123456789012345678, None)
        assert url.startswith("https://cdn.discordapp.com/embed/avatars/")


class TestMoreInfoUrl:
    def test_builds_members_deep_link(self) -> None:
        assert (
            more_info_url("https://sidequest-guild.com", 42)
            == "https://sidequest-guild.com/officers/#members?member=42"
        )

    def test_strips_trailing_slash(self) -> None:
        assert (
            more_info_url("https://sidequest-guild.com/", 42)
            == "https://sidequest-guild.com/officers/#members?member=42"
        )


class TestClickerIsGm:
    def test_holds_gm_role(self) -> None:
        assert clicker_is_gm({10, 20}, [20, 30]) is True

    def test_lacks_gm_role(self) -> None:
        assert clicker_is_gm({10}, [20, 30]) is False

    def test_no_configured_gm_roles(self) -> None:
        assert clicker_is_gm({10}, []) is False


class TestBuildNoticeEmbed:
    def test_includes_name_and_status(self) -> None:
        embed = build_notice_embed(_candidate())
        assert embed.title == "New signature in the Ledger"
        assert "Jaina" in embed.description
        assert "@jaina.proudmoore" in embed.description
        status_field = next(f for f in embed.fields if f.name == STATUS_FIELD_NAME)
        assert status_field.value == GUEST_STATUS_TEXT

    def test_includes_battletag_when_linked(self) -> None:
        embed = build_notice_embed(_candidate(battletag="Jaina#1234"))
        battletag_field = next(f for f in embed.fields if f.name == "BattleTag")
        assert battletag_field.value == "Jaina#1234"

    def test_omits_battletag_when_unlinked(self) -> None:
        embed = build_notice_embed(_candidate(battletag=None))
        assert all(f.name != "BattleTag" for f in embed.fields)


class TestApplyApprovalStatus:
    def test_replaces_existing_status_field(self) -> None:
        embed = build_notice_embed(_candidate())
        updated = apply_approval_status(embed, "Approved by @gm")
        status_field = next(f for f in updated.fields if f.name == STATUS_FIELD_NAME)
        assert status_field.value == "Approved by @gm"
        assert sum(1 for f in updated.fields if f.name == STATUS_FIELD_NAME) == 1

    def test_appends_status_field_when_missing(self) -> None:
        embed = discord.Embed(title="Bare")
        updated = apply_approval_status(embed, "Approved by @gm")
        status_field = next(f for f in updated.fields if f.name == STATUS_FIELD_NAME)
        assert status_field.value == "Approved by @gm"


class TestBuildNoticeView:
    def test_has_approve_and_link_buttons(self) -> None:
        view = build_notice_view(1, "https://sidequest-guild.com/officers/#members?member=1")
        approve = next(item for item in view.children if getattr(item, "custom_id", None) == build_custom_id(1))
        link = next(item for item in view.children if getattr(item, "url", None))
        assert approve.item.disabled is False
        assert link.url == "https://sidequest-guild.com/officers/#members?member=1"

    def test_approved_disables_approve_button(self) -> None:
        view = build_notice_view(1, "https://sidequest-guild.com/officers/#members?member=1", approved=True)
        approve = next(item for item in view.children if getattr(item, "custom_id", None) == build_custom_id(1))
        assert approve.item.disabled is True
