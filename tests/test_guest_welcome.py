"""Unit tests for the guest-welcome plugin: the shared embed, the DM, and the persistent button."""

import logging
from unittest.mock import AsyncMock, MagicMock, Mock

import discord
import pytest

from sidequest_bot.plugins.guest_welcome import (
    CUSTOM_ID,
    CUSTOM_ID_RE,
    GuestWelcomeButton,
    build_guest_welcome_embed,
    build_guest_welcome_view,
    send_guest_welcome_dm,
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


class TestSendGuestWelcomeDm:
    async def test_sends_the_shared_embed(self) -> None:
        member = _fake_member()

        await send_guest_welcome_dm(member)

        member.send.assert_awaited_once()
        embed = member.send.await_args.kwargs["embed"]
        assert "Pix" in embed.description

    async def test_forbidden_dm_is_only_logged_at_info(self, caplog: pytest.LogCaptureFixture) -> None:
        member = _fake_member()
        member.send = AsyncMock(side_effect=discord.Forbidden(MagicMock(status=403), "Cannot send messages"))

        with caplog.at_level(logging.INFO):
            await send_guest_welcome_dm(member)

        assert any(r.levelno == logging.INFO and "DMs are closed" in r.message for r in caplog.records)
        assert not any(r.levelno >= logging.WARNING for r in caplog.records)

    async def test_other_http_errors_are_logged_at_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        member = _fake_member()
        member.send = AsyncMock(side_effect=discord.HTTPException(MagicMock(status=500), "boom"))

        with caplog.at_level(logging.WARNING):
            await send_guest_welcome_dm(member)

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


class TestGuestWelcomeButtonCallback:
    async def test_replies_ephemerally_with_the_shared_embed(self) -> None:
        button = GuestWelcomeButton()
        interaction = Mock(spec=discord.Interaction)
        interaction.user = _fake_member()
        interaction.response = Mock()
        interaction.response.send_message = AsyncMock()

        await button.callback(interaction)

        interaction.response.send_message.assert_awaited_once()
        _, kwargs = interaction.response.send_message.call_args
        assert kwargs["ephemeral"] is True
        assert "Pix" in kwargs["embed"].description

    async def test_links_the_class_channel_when_it_exists(self) -> None:
        button = GuestWelcomeButton()
        channel = _fake_channel("choose-your-class")
        interaction = Mock(spec=discord.Interaction)
        interaction.user = _fake_member(text_channels=[channel])
        interaction.response = Mock()
        interaction.response.send_message = AsyncMock()

        await button.callback(interaction)

        _, kwargs = interaction.response.send_message.call_args
        assert channel.mention in kwargs["embed"].description

    async def test_noop_when_invoker_is_not_a_member(self) -> None:
        button = GuestWelcomeButton()
        interaction = Mock(spec=discord.Interaction)
        interaction.user = Mock(spec=discord.User)  # a DM invocation, not a guild member
        interaction.response = Mock()
        interaction.response.send_message = AsyncMock()

        await button.callback(interaction)

        interaction.response.send_message.assert_not_awaited()

    async def test_from_custom_id_rebuilds_a_fresh_button(self) -> None:
        rebuilt = await GuestWelcomeButton.from_custom_id(Mock(), Mock(), Mock())
        assert isinstance(rebuilt, GuestWelcomeButton)
        assert rebuilt.item.custom_id == CUSTOM_ID
