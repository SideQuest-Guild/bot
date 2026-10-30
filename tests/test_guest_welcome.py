"""Unit tests for the guest-welcome plugin: the shared embed, the DM, and the persistent button."""

import logging
from unittest.mock import AsyncMock, MagicMock, Mock

import discord
import pytest

from sidequest_bot.api_client import APIError
from sidequest_bot.plugins.guest_welcome import (
    CUSTOM_ID,
    CUSTOM_ID_RE,
    GuestWelcomeButton,
    _recruiting_field_text,
    build_guest_welcome_embed,
    build_guest_welcome_view,
    send_guest_welcome_dm,
)
from sidequest_common.schemas import (
    Recruiting,
    RecruitingClassWant,
    RecruitingPriority,
    RecruitingRoleWant,
    SignupRole,
    WowClass,
)


def _fake_channel(name: str, *, channel_id: int = 999) -> MagicMock:
    channel = MagicMock(spec=discord.TextChannel)
    channel.name = name
    channel.mention = f"<#{channel_id}>"
    return channel


def _fake_member(*, display_name: str = "Pix", text_channels: list[MagicMock] | None = None) -> MagicMock:
    member = MagicMock(spec=discord.Member)
    member.id = 1
    member.display_name = display_name
    member.guild = MagicMock(spec=discord.Guild)
    member.guild.id = 42
    member.guild.text_channels = text_channels or []
    member.send = AsyncMock()
    return member


def _fake_api(recruiting: Recruiting | None = None) -> Mock:
    api = Mock()
    if recruiting is None:
        api.get_recruiting = AsyncMock(side_effect=APIError(503, "unreachable"))
    else:
        api.get_recruiting = AsyncMock(return_value=recruiting)
    return api


EMPTY_RECRUITING = Recruiting(roles=[], classes=[], updated_at=None)


class TestBuildGuestWelcomeEmbed:
    def test_addresses_the_member_by_display_name(self) -> None:
        embed = build_guest_welcome_embed(_fake_member(display_name="Pix"))
        assert "Pix" in embed.description

    def test_mentions_guest_status_and_class_channel_and_events(self) -> None:
        embed = build_guest_welcome_embed(_fake_member())
        assert "guest" in embed.description.lower()
        assert "Quest Master" in embed.description
        assert "#choose-your-class" in embed.description
        assert "https://sidequest-guild.com/events/" in embed.description

    def test_links_the_class_channel_when_it_exists(self) -> None:
        channel = _fake_channel("choose-your-class")
        embed = build_guest_welcome_embed(_fake_member(text_channels=[channel]))
        assert channel.mention in embed.description
        assert "#choose-your-class" not in embed.description

    def test_falls_back_to_plain_text_when_the_channel_is_missing(self) -> None:
        embed = build_guest_welcome_embed(_fake_member(text_channels=[_fake_channel("general")]))
        assert "#choose-your-class" in embed.description

    def test_no_in_game_invite_instructions(self) -> None:
        embed = build_guest_welcome_embed(_fake_member())
        assert "invite" not in embed.description.lower()

    def test_no_field_when_recruiting_is_none(self) -> None:
        embed = build_guest_welcome_embed(_fake_member())
        assert embed.fields == []

    def test_no_field_when_nothing_is_wanted(self) -> None:
        embed = build_guest_welcome_embed(_fake_member(), EMPTY_RECRUITING)
        assert embed.fields == []

    def test_field_lists_wanted_roles_and_classes(self) -> None:
        recruiting = Recruiting(
            roles=[RecruitingRoleWant(role="tank", priority=RecruitingPriority.HIGH, note="semi-core")],
            classes=[RecruitingClassWant(wow_class="druid", priority=RecruitingPriority.NORMAL, note=None)],
            updated_at=None,
        )
        embed = build_guest_welcome_embed(_fake_member(), recruiting)
        (field,) = embed.fields
        assert field.name == "We're looking for"
        assert "**Tank**" in field.value
        assert "semi-core" in field.value
        assert "Druid" in field.value
        assert "**Druid**" not in field.value


class TestSendGuestWelcomeDm:
    async def test_sends_the_shared_embed(self) -> None:
        member = _fake_member()

        await send_guest_welcome_dm(member, _fake_api(EMPTY_RECRUITING))

        member.send.assert_awaited_once()
        embed = member.send.await_args.kwargs["embed"]
        assert "Pix" in embed.description

    async def test_includes_recruiting_field_when_set(self) -> None:
        member = _fake_member()
        recruiting = Recruiting(roles=[RecruitingRoleWant(role="healer")], classes=[], updated_at=None)

        await send_guest_welcome_dm(member, _fake_api(recruiting))

        embed = member.send.await_args.kwargs["embed"]
        assert embed.fields[0].name == "We're looking for"

    async def test_omits_recruiting_field_when_the_api_call_fails(self) -> None:
        member = _fake_member()

        await send_guest_welcome_dm(member, _fake_api(None))

        embed = member.send.await_args.kwargs["embed"]
        assert embed.fields == []

    async def test_forbidden_dm_is_only_logged_at_info(self, caplog: pytest.LogCaptureFixture) -> None:
        member = _fake_member()
        member.send = AsyncMock(side_effect=discord.Forbidden(MagicMock(status=403), "Cannot send messages"))

        with caplog.at_level(logging.INFO):
            await send_guest_welcome_dm(member, _fake_api(EMPTY_RECRUITING))

        assert any(r.levelno == logging.INFO and "DMs are closed" in r.message for r in caplog.records)
        assert not any(r.levelno >= logging.WARNING for r in caplog.records)

    async def test_other_http_errors_are_logged_at_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        member = _fake_member()
        member.send = AsyncMock(side_effect=discord.HTTPException(MagicMock(status=500), "boom"))

        with caplog.at_level(logging.WARNING):
            await send_guest_welcome_dm(member, _fake_api(EMPTY_RECRUITING))

        assert any(r.levelno == logging.WARNING for r in caplog.records)


class TestCustomId:
    def test_matches_the_static_custom_id(self) -> None:
        assert CUSTOM_ID_RE.match(CUSTOM_ID) is not None

    def test_does_not_match_unrelated_ids(self) -> None:
        assert CUSTOM_ID_RE.match("sq:rm:some:thing") is None


class TestBuildGuestWelcomeView:
    def test_has_one_persistent_button(self) -> None:
        view = build_guest_welcome_view()
        assert len(view.children) == 1
        assert view.timeout is None
        (button,) = view.children
        assert button.item.custom_id == CUSTOM_ID


def _fake_interaction(*, user: Mock, recruiting: Recruiting | None = EMPTY_RECRUITING) -> Mock:
    interaction = Mock(spec=discord.Interaction)
    interaction.user = user
    interaction.client = Mock()
    interaction.client.api = _fake_api(recruiting)
    interaction.response = Mock()
    interaction.response.send_message = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.followup = Mock()
    interaction.followup.send = AsyncMock()
    return interaction


class TestGuestWelcomeButtonCallback:
    async def test_defers_ephemerally_before_fetching_recruiting(self) -> None:
        button = GuestWelcomeButton()
        interaction = _fake_interaction(user=_fake_member())

        await button.callback(interaction)

        interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)

    async def test_replies_ephemerally_with_the_shared_embed_via_followup(self) -> None:
        button = GuestWelcomeButton()
        interaction = _fake_interaction(user=_fake_member())

        await button.callback(interaction)

        interaction.followup.send.assert_awaited_once()
        interaction.response.send_message.assert_not_awaited()
        _, kwargs = interaction.followup.send.call_args
        assert kwargs["ephemeral"] is True
        assert "Pix" in kwargs["embed"].description

    async def test_links_the_class_channel_when_it_exists(self) -> None:
        button = GuestWelcomeButton()
        channel = _fake_channel("choose-your-class")
        interaction = _fake_interaction(user=_fake_member(text_channels=[channel]))

        await button.callback(interaction)

        _, kwargs = interaction.followup.send.call_args
        assert channel.mention in kwargs["embed"].description

    async def test_includes_recruiting_field_when_set(self) -> None:
        button = GuestWelcomeButton()
        recruiting = Recruiting(roles=[RecruitingRoleWant(role="dps")], classes=[], updated_at=None)
        interaction = _fake_interaction(user=_fake_member(), recruiting=recruiting)

        await button.callback(interaction)

        _, kwargs = interaction.followup.send.call_args
        assert kwargs["embed"].fields[0].name == "We're looking for"

    async def test_noop_when_invoker_is_not_a_member(self) -> None:
        button = GuestWelcomeButton()
        interaction = _fake_interaction(user=Mock(spec=discord.User))  # a DM invocation, not a guild member

        await button.callback(interaction)

        interaction.response.defer.assert_not_awaited()
        interaction.followup.send.assert_not_awaited()

    async def test_from_custom_id_rebuilds_a_fresh_button(self) -> None:
        rebuilt = await GuestWelcomeButton.from_custom_id(Mock(), Mock(), Mock())
        assert isinstance(rebuilt, GuestWelcomeButton)
        assert rebuilt.item.custom_id == CUSTOM_ID


class TestRecruitingFieldText:
    def test_bounds_to_1024_chars_with_max_length_notes_on_every_role_and_class(self) -> None:
        """3 roles + 9 classes, each with the max 140-char note, comfortably exceeds Discord's 1,024-char limit."""
        note = "x" * 140
        roles = [RecruitingRoleWant(role=role, priority=RecruitingPriority.HIGH, note=note) for role in SignupRole]
        classes = [
            RecruitingClassWant(wow_class=wow_class, priority=RecruitingPriority.HIGH, note=note)
            for wow_class in WowClass
        ]
        recruiting = Recruiting(roles=roles, classes=classes, updated_at=None)

        text = _recruiting_field_text(recruiting)

        assert text is not None
        assert len(text) <= 1024
        assert text.endswith("…")

    def test_embed_with_max_length_recruiting_stays_within_field_and_total_limits(self) -> None:
        note = "x" * 140
        roles = [RecruitingRoleWant(role=role, priority=RecruitingPriority.HIGH, note=note) for role in SignupRole]
        classes = [
            RecruitingClassWant(wow_class=wow_class, priority=RecruitingPriority.HIGH, note=note)
            for wow_class in WowClass
        ]
        recruiting = Recruiting(roles=roles, classes=classes, updated_at=None)

        embed = build_guest_welcome_embed(_fake_member(), recruiting)

        (field,) = embed.fields
        assert len(field.value) <= 1024
        assert len(embed) < 6000
