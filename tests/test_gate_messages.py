"""Unit tests for the gate layout messages plugin: rendering, sync, and the manual review request flow."""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock

import discord
import pytest

from sidequest_bot.plugins import gate_messages
from sidequest_bot.plugins.gate_messages import (
    STATUS_FIELD_NAME,
    GateConfirmButton,
    GateMessagesCog,
    apply_review_status,
    build_guest_view,
    build_ledger_view,
    build_review_embed,
)
from sidequest_bot.plugins.link_roles import LinkRolesCog
from sidequest_common.schemas import MemberLink
from tests import fakes
from tests.fakes import named, sidequest_server


def _config(*, roster_role_id: int | None = None, battlenet_role_id: int | None = None) -> SimpleNamespace:
    return SimpleNamespace(roster_role_id=roster_role_id, battlenet_role_id=battlenet_role_id)


def _server() -> tuple[MagicMock, SimpleNamespace]:
    guild = sidequest_server()
    config = _config(
        roster_role_id=named(guild.roles, "New Arrival").id,
        battlenet_role_id=named(guild.roles, "Battle-Tagged").id,
    )
    return guild, config


def _all_text(view: discord.ui.LayoutView) -> str:
    """Every ``TextDisplay``/``Section`` string in ``view``, concatenated, for substring assertions."""
    chunks: list[str] = []

    def walk(item: Any) -> None:
        if isinstance(item, discord.ui.TextDisplay):
            chunks.append(item.content)
        elif isinstance(item, (discord.ui.Section, discord.ui.Container, discord.ui.LayoutView)):
            for child in item.children:
                walk(child)

    walk(view)
    return "\n".join(chunks)


def _all_buttons(view: discord.ui.LayoutView) -> list[discord.ui.Button]:
    buttons: list[discord.ui.Button] = []

    def walk(item: Any) -> None:
        if isinstance(item, discord.ui.Button):
            buttons.append(item)
        elif isinstance(item, discord.ui.DynamicItem) and isinstance(item.item, discord.ui.Button):
            buttons.append(item.item)
        elif isinstance(item, discord.ui.ActionRow | discord.ui.Container | discord.ui.LayoutView):
            for child in item.children:
                walk(child)

    walk(view)
    return buttons


class TestBuildLedgerView:
    def test_copy_reflects_automatic_confirmation_and_manual_fallback(self) -> None:
        guild, config = _server()
        text = _all_text(build_ledger_view(guild, config))
        assert "link Battle.net on your account page and you're confirmed automatically within about 30 minutes" in text
        assert "or an officer confirms you" in text

    def test_has_rather_not_sign_in_section_and_button(self) -> None:
        guild, config = _server()
        view = build_ledger_view(guild, config)
        text = _all_text(view)
        assert "Rather not sign in?" in text
        assert "an officer will let you in by hand" in text
        buttons = _all_buttons(view)
        review = next(b for b in buttons if b.label == "I'd rather not sign in")
        assert review.custom_id == gate_messages.REVIEW_CUSTOM_ID

    def test_keeps_18_plus_footer_and_link_buttons(self) -> None:
        guild, config = _server()
        view = build_ledger_view(guild, config)
        text = _all_text(view)
        assert "18 and older" in text
        labels = {b.label for b in _all_buttons(view)}
        assert {"Sign the Ledger", "Guild rules", "Privacy", "Terms"} <= labels


class TestBuildGuestView:
    def test_copy_reflects_automatic_confirmation(self) -> None:
        guild, config = _server()
        text = _all_text(build_guest_view(guild, config))
        assert "linking Battle.net confirms you automatically within about 30 minutes" in text
        assert "an officer confirms you" in text

    def test_drops_find_you_faster_phrase(self) -> None:
        guild, config = _server()
        text = _all_text(build_guest_view(guild, config))
        assert "so officers can find you faster" not in text
        assert "Link Battle.net on your account page." in text


class TestReduce:
    def test_ignores_ids_and_media_urls(self) -> None:
        first = {"type": 12, "id": 1, "items": [{"media": {"url": "https://cdn.example/a.png"}}]}
        second = {"type": 12, "id": 2, "items": [{"media": {"url": "https://cdn.example/b.png?ex=123"}}]}
        assert gate_messages._reduce(first) == gate_messages._reduce(second)

    def test_text_changes_are_detected(self) -> None:
        first = {"type": 10, "id": 1, "content": "Hello"}
        second = {"type": 10, "id": 2, "content": "Goodbye"}
        assert gate_messages._reduce(first) != gate_messages._reduce(second)


class _FakeRawComponent:
    """Stands in for a ``discord.Component`` fetched from history: ``.type`` plus a fixed ``to_dict()``."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.type = discord.ComponentType(payload["type"])

    def to_dict(self) -> dict[str, Any]:
        return self._payload


def _fake_layout_message(view: discord.ui.LayoutView, *, author_id: int) -> MagicMock:
    message = MagicMock(spec=discord.Message)
    message.author = SimpleNamespace(id=author_id)
    message.flags = SimpleNamespace(components_v2=True)
    message.components = [_FakeRawComponent(payload) for payload in view.to_components()]
    message.edit = AsyncMock()
    message.pin = AsyncMock()
    return message


def _history(messages: list[MagicMock]) -> Any:
    async def gen(**_kwargs: Any) -> Any:
        for message in messages:
            yield message

    return gen


def _make_cog(*, api: object, configs: object, guild: object) -> GateMessagesCog:
    cog = GateMessagesCog.__new__(GateMessagesCog)
    cog.bot = SimpleNamespace(
        api=api,
        configs=configs,
        user=SimpleNamespace(id=fakes.BOT_USER_ID),
        get_guild=lambda gid: guild if gid == guild.id else None,
    )
    cog._review_cooldowns = {}
    return cog


class TestSyncChannel:
    async def test_posts_when_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        guild, config = _server()
        monkeypatch.setattr(gate_messages, "_web_settings", lambda: SimpleNamespace(GUILD_ID=guild.id))
        channel = named(guild.channels, "guest-chat")
        channel.history = _history([])
        sent = MagicMock(spec=discord.Message)
        sent.pin = AsyncMock()
        channel.send = AsyncMock(return_value=sent)
        configs = AsyncMock()
        configs.get.return_value = config
        cog = _make_cog(api=AsyncMock(), configs=configs, guild=guild)

        await cog._sync_loop.coro(cog)

        channel.send.assert_awaited_once()
        kwargs = channel.send.await_args.kwargs
        assert isinstance(kwargs["view"], discord.ui.LayoutView)
        assert len(kwargs["files"]) == 2
        assert kwargs["allowed_mentions"].to_dict() == discord.AllowedMentions.none().to_dict()
        sent.pin.assert_awaited_once()  # guest-chat's layout message is pinned

    async def test_unchanged_message_is_left_alone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        guild, config = _server()
        monkeypatch.setattr(gate_messages, "_web_settings", lambda: SimpleNamespace(GUILD_ID=guild.id))
        channel = named(guild.channels, "sign-the-ledger")
        view = build_ledger_view(guild, config)
        message = _fake_layout_message(view, author_id=fakes.BOT_USER_ID)
        channel.history = _history([message])
        channel.send = AsyncMock()
        configs = AsyncMock()
        configs.get.return_value = config
        cog = _make_cog(api=AsyncMock(), configs=configs, guild=guild)

        await cog._sync_loop.coro(cog)

        message.edit.assert_not_called()
        channel.send.assert_not_called()

    async def test_changed_message_is_edited_in_place_with_its_images(self, monkeypatch: pytest.MonkeyPatch) -> None:
        guild, config = _server()
        monkeypatch.setattr(gate_messages, "_web_settings", lambda: SimpleNamespace(GUILD_ID=guild.id))
        channel = named(guild.channels, "sign-the-ledger")
        stale_view = build_ledger_view(guild, _config())  # no roster/battlenet roles configured: different text
        message = _fake_layout_message(stale_view, author_id=fakes.BOT_USER_ID)
        channel.history = _history([message])
        channel.send = AsyncMock()
        configs = AsyncMock()
        configs.get.return_value = config
        cog = _make_cog(api=AsyncMock(), configs=configs, guild=guild)

        await cog._sync_loop.coro(cog)

        message.edit.assert_awaited_once()
        edit_kwargs = message.edit.await_args.kwargs
        assert {file.filename for file in edit_kwargs["attachments"]} == {
            "sidequest-ledger.webp",
            "sidequest-crest.png",
        }
        assert isinstance(edit_kwargs["view"], discord.ui.LayoutView)
        channel.send.assert_not_called()


def _member(member_id: int, roles: list[MagicMock]) -> MagicMock:
    member = fakes.make_member(member_id, "traveller", roles)
    member.created_at = datetime(2024, 1, 1, tzinfo=UTC)
    member.joined_at = datetime(2024, 6, 1, tzinfo=UTC)
    return member


def _interaction(*, guild: MagicMock, member: MagicMock) -> Mock:
    interaction = Mock()
    interaction.guild = guild
    interaction.user = member
    interaction.followup.send = AsyncMock()
    return interaction


class TestHandleReviewSubmit:
    async def test_already_holds_roster_role_stops_without_posting(self) -> None:
        guild, config = _server()
        new_arrival = named(guild.roles, "New Arrival")
        member = _member(500, [guild.roles[0], new_arrival])
        channel = named(guild.channels, "officer-chat")
        channel.send = AsyncMock()
        configs = AsyncMock()
        configs.get.return_value = config
        cog = _make_cog(api=AsyncMock(), configs=configs, guild=guild)
        interaction = _interaction(guild=guild, member=member)

        await cog.handle_review_submit(interaction, "Nox Zul", None)

        channel.send.assert_not_called()
        assert "already confirmed" in interaction.followup.send.await_args.args[0].lower()

    async def test_already_confirmed_per_api_link_stops_without_posting(self) -> None:
        guild, config = _server()
        member = _member(501, [guild.roles[0]])
        channel = named(guild.channels, "officer-chat")
        channel.send = AsyncMock()
        api = AsyncMock()
        api.list_member_links.return_value = [
            MemberLink(discord_id=501, battlenet_linked=True, roster_confirmed=True, signed_in=False)
        ]
        configs = AsyncMock()
        configs.get.return_value = config
        cog = _make_cog(api=api, configs=configs, guild=guild)
        interaction = _interaction(guild=guild, member=member)

        await cog.handle_review_submit(interaction, "Nox Zul", None)

        channel.send.assert_not_called()
        assert "already" in interaction.followup.send.await_args.args[0].lower()

    async def test_cooldown_blocks_a_second_request(self) -> None:
        guild, config = _server()
        member = _member(502, [guild.roles[0]])
        channel = named(guild.channels, "officer-chat")
        channel.send = AsyncMock()
        api = AsyncMock()
        api.list_member_links.return_value = []
        configs = AsyncMock()
        configs.get.return_value = config
        cog = _make_cog(api=api, configs=configs, guild=guild)
        interaction = _interaction(guild=guild, member=member)
        await cog.handle_review_submit(interaction, "Nox Zul", None)
        channel.send.assert_awaited_once()
        channel.send.reset_mock()
        interaction.followup.send.reset_mock()

        await cog.handle_review_submit(interaction, "Nox Zul", None)

        channel.send.assert_not_called()
        assert "already asked" in interaction.followup.send.await_args.args[0].lower()

    async def test_success_posts_once_to_officer_chat_without_pings(self) -> None:
        guild, config = _server()
        member = _member(503, [guild.roles[0]])
        channel = named(guild.channels, "officer-chat")
        channel.send = AsyncMock()
        api = AsyncMock()
        api.list_member_links.return_value = []
        configs = AsyncMock()
        configs.get.return_value = config
        cog = _make_cog(api=api, configs=configs, guild=guild)
        interaction = _interaction(guild=guild, member=member)

        await cog.handle_review_submit(interaction, "Nox Zul", "Please let me in")

        channel.send.assert_awaited_once()
        kwargs = channel.send.await_args.kwargs
        assert kwargs["allowed_mentions"].to_dict() == discord.AllowedMentions.none().to_dict()
        assert kwargs["embed"].title == "Manual review request"
        assert "sent to the officers" in interaction.followup.send.await_args.args[0].lower()


class TestGateConfirmButton:
    async def test_rejects_non_gm_clicker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        guild, _config = _server()
        clicker = _member(600, [guild.roles[0]])  # no GM role
        interaction = Mock()
        interaction.response.defer = AsyncMock()
        interaction.guild = guild
        interaction.user = clicker
        interaction.followup.send = AsyncMock()
        monkeypatch.setattr(gate_messages, "_web_settings", lambda: SimpleNamespace(GM_ROLE_IDS=[999999]))

        await GateConfirmButton(999).callback(interaction)

        interaction.followup.send.assert_awaited_once()
        assert "quest master" in interaction.followup.send.await_args.args[0].lower()
        interaction.client.get_cog.assert_not_called()

    async def test_gm_calls_confirm_path_and_marks_message_resolved(self, monkeypatch: pytest.MonkeyPatch) -> None:
        guild, _config = _server()
        quest_master = named(guild.roles, "Quest Master")
        clicker = _member(601, [guild.roles[0], quest_master])
        target = _member(602, [guild.roles[0]])
        guild.get_member.side_effect = lambda mid: target if mid == 602 else None
        member_link = MemberLink(discord_id=602, battlenet_linked=False, roster_confirmed=True, signed_in=False)
        link_cog = LinkRolesCog.__new__(LinkRolesCog)
        link_cog.confirm_discord_member = AsyncMock(return_value=member_link)
        message = MagicMock(spec=discord.Message)
        message.embeds = [build_review_embed(target, "Nox Zul", None)]
        message.edit = AsyncMock()
        interaction = Mock()
        interaction.response.defer = AsyncMock()
        interaction.guild = guild
        interaction.user = clicker
        interaction.message = message
        interaction.followup.send = AsyncMock()

        def get_cog(name: str) -> object:
            return link_cog if name == "LinkRolesCog" else None

        interaction.client.get_cog.side_effect = get_cog
        monkeypatch.setattr(gate_messages, "_web_settings", lambda: SimpleNamespace(GM_ROLE_IDS=[quest_master.id]))

        await GateConfirmButton(602).callback(interaction)

        link_cog.confirm_discord_member.assert_awaited_once_with(interaction, target, clicker)
        message.edit.assert_awaited_once()
        updated_embed = message.edit.await_args.kwargs["embed"]
        status = next(f.value for f in updated_embed.fields if f.name == STATUS_FIELD_NAME)
        assert status == f"Confirmed by {clicker.mention}"
        new_view = message.edit.await_args.kwargs["view"]
        confirm_button = next(item for item in new_view.children if isinstance(item, GateConfirmButton))
        assert confirm_button.item.disabled is True

    async def test_already_confirmed_is_idempotent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        guild, _config = _server()
        quest_master = named(guild.roles, "Quest Master")
        clicker = _member(603, [guild.roles[0], quest_master])
        message = MagicMock(spec=discord.Message)
        embed = build_review_embed(_member(604, [guild.roles[0]]), "Nox Zul", None)
        apply_review_status(embed, "Confirmed by Someone")
        message.embeds = [embed]
        interaction = Mock()
        interaction.response.defer = AsyncMock()
        interaction.guild = guild
        interaction.user = clicker
        interaction.message = message
        interaction.followup.send = AsyncMock()
        monkeypatch.setattr(gate_messages, "_web_settings", lambda: SimpleNamespace(GM_ROLE_IDS=[quest_master.id]))

        await GateConfirmButton(604).callback(interaction)

        assert "already confirmed" in interaction.followup.send.await_args.args[0].lower()
        interaction.client.get_cog.assert_not_called()

    async def test_target_left_server_marks_message_and_does_not_confirm(self, monkeypatch: pytest.MonkeyPatch) -> None:
        guild, _config = _server()
        quest_master = named(guild.roles, "Quest Master")
        clicker = _member(605, [guild.roles[0], quest_master])
        guild.get_member.side_effect = lambda _mid: None
        message = MagicMock(spec=discord.Message)
        message.embeds = [build_review_embed(_member(606, [guild.roles[0]]), "Nox Zul", None)]
        message.edit = AsyncMock()
        interaction = Mock()
        interaction.response.defer = AsyncMock()
        interaction.guild = guild
        interaction.user = clicker
        interaction.message = message
        interaction.followup.send = AsyncMock()
        monkeypatch.setattr(gate_messages, "_web_settings", lambda: SimpleNamespace(GM_ROLE_IDS=[quest_master.id]))

        await GateConfirmButton(606).callback(interaction)

        message.edit.assert_awaited_once()
        updated_embed = message.edit.await_args.kwargs["embed"]
        status = next(f.value for f in updated_embed.fields if f.name == STATUS_FIELD_NAME)
        assert status == gate_messages.LEFT_SERVER_STATUS_TEXT
        assert "left the server" in interaction.followup.send.await_args.args[0].lower()
        interaction.client.get_cog.assert_not_called()
