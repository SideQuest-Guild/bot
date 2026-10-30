"""Unit tests for the badges plugin: role creation/recreation, role diffing, announcements, and the sync loop."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import discord

from sidequest_bot.api_client import APIError, NotFoundError
from sidequest_bot.plugins.badges import (
    BADGE_ICON_FILENAME,
    ROLE_ICONS_FEATURE,
    BadgesCog,
    badge_grant_reason,
    badge_icon,
    build_badge_embed,
    create_badge_role,
    hall_of_heroes_url,
    plan_badge_role_changes,
)
from sidequest_common.schemas import BadgeAck, BadgeRoleState, BadgeSyncReport, PendingBadgeAnnouncement
from sidequest_common.schemas.badges import BADGE_LIST


def _config(*, badge_channel_id: int | None = None, badge_role_ids: dict[str, int] | None = None) -> SimpleNamespace:
    return SimpleNamespace(badge_channel_id=badge_channel_id, badge_role_ids=badge_role_ids or {})


def _fake_role(role_id: int, *, permissions: discord.Permissions | None = None) -> MagicMock:
    role = MagicMock(spec=discord.Role)
    role.id = role_id
    role.mention = f"<@&{role_id}>"
    role.is_default.return_value = False
    role.managed = False
    role.is_assignable.return_value = True
    role.permissions = permissions if permissions is not None else discord.Permissions.none()
    return role


def _fake_guild(*, guild_id: int = 42, roles: list[MagicMock] | None = None) -> MagicMock:
    guild = MagicMock(spec=discord.Guild)
    guild.id = guild_id
    roles_by_id = {role.id: role for role in (roles or [])}
    guild.get_role.side_effect = roles_by_id.get
    guild.get_member.return_value = None
    guild.get_channel.return_value = None
    guild.create_role = AsyncMock()
    guild.members = []
    return guild


def _fake_member(member_id: int, *, role_ids: list[int] | None = None) -> MagicMock:
    member = MagicMock(spec=discord.Member)
    member.id = member_id
    member.roles = [MagicMock(id=role_id) for role_id in (role_ids or [])]
    member.add_roles = AsyncMock()
    member.remove_roles = AsyncMock()
    return member


def _fake_channel(channel_id: int = 900) -> MagicMock:
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = channel_id
    channel.send = AsyncMock()
    return channel


def _announcement(**overrides: object) -> PendingBadgeAnnouncement:
    base: dict[str, object] = {
        "discord_id": 1,
        "badge_id": "raid_regular",
        "name": "Raid Regular",
        "description": "Attended 10 raids with the guild.",
    }
    base.update(overrides)
    return PendingBadgeAnnouncement.model_validate(base)


def _make_cog(*, api: object | None = None, configs: object | None = None, guilds: list[object] | None = None):
    cog = BadgesCog.__new__(BadgesCog)
    cog.bot = SimpleNamespace(api=api or AsyncMock(), configs=configs or AsyncMock(), guilds=guilds or [])
    cog._warned_role_ids = set()
    return cog


class TestPlanBadgeRoleChanges:
    """Pure: only ever touches ids present in badge_role_ids, driven entirely by the API's role_state."""

    def test_adds_role_for_newly_earned_badge(self) -> None:
        to_add, to_remove = plan_badge_role_changes(set(), {"first_steps"}, {"first_steps": 100})
        assert to_add == [100]
        assert to_remove == []

    def test_no_change_when_already_matching(self) -> None:
        to_add, to_remove = plan_badge_role_changes({100}, {"first_steps"}, {"first_steps": 100})
        assert to_add == []
        assert to_remove == []

    def test_removes_role_no_longer_in_wanted_set(self) -> None:
        """Covers Top Helper moving to a new winner: the old winner's role_state drops top_helper."""
        to_add, to_remove = plan_badge_role_changes({100}, set(), {"top_helper": 100})
        assert to_add == []
        assert to_remove == [100]

    def test_adds_top_helper_for_new_winner(self) -> None:
        to_add, to_remove = plan_badge_role_changes(set(), {"top_helper"}, {"top_helper": 100})
        assert to_add == [100]
        assert to_remove == []

    def test_ignores_role_ids_not_in_badge_role_ids(self) -> None:
        """A member's unrelated roles never appear in either list."""
        to_add, to_remove = plan_badge_role_changes({999}, {"first_steps"}, {"first_steps": 100})
        assert to_add == [100]
        assert to_remove == []

    def test_permanent_badge_never_removed_when_present_in_wanted_set(self) -> None:
        _to_add, to_remove = plan_badge_role_changes({100}, {"first_steps"}, {"first_steps": 100})
        assert 100 not in to_remove


class TestBadgeGrantReason:
    def test_uses_badge_display_name(self) -> None:
        assert badge_grant_reason("raid_regular") == "Earned Raid Regular on sidequest-guild.com"

    def test_falls_back_to_badge_id_for_unknown_badge(self) -> None:
        assert badge_grant_reason("mystery") == "Earned mystery on sidequest-guild.com"


class TestHallOfHeroesUrl:
    def test_strips_trailing_slash_from_base(self) -> None:
        assert hall_of_heroes_url("https://sidequest-guild.com/") == "https://sidequest-guild.com/heroes/"

    def test_no_member_id_in_payload_so_link_is_generic(self) -> None:
        """PendingBadgeAnnouncement carries no member_id, so every announcement links the same landing page."""
        assert hall_of_heroes_url("https://sidequest-guild.com") == "https://sidequest-guild.com/heroes/"


class TestBuildBadgeEmbed:
    def test_includes_mention_name_and_description(self) -> None:
        embed = build_badge_embed(_announcement(), "https://sidequest-guild.com/heroes/", with_icon=True)
        assert "<@1>" in embed.description
        assert "Raid Regular" in embed.description
        assert "Attended 10 raids with the guild." in embed.description

    def test_includes_hall_of_heroes_link_field(self) -> None:
        embed = build_badge_embed(_announcement(), "https://sidequest-guild.com/heroes/", with_icon=True)
        link_field = next(f for f in embed.fields if f.name == "Hall of Heroes")
        assert "https://sidequest-guild.com/heroes/" in link_field.value

    def test_badge_image_is_the_thumbnail_and_replaces_the_emoji(self) -> None:
        with_icon = build_badge_embed(_announcement(), "https://x/heroes/", with_icon=True)
        without = build_badge_embed(_announcement(), "https://x/heroes/", with_icon=False)
        assert with_icon.thumbnail.url == f"attachment://{BADGE_ICON_FILENAME}"
        assert with_icon.title == "Badge earned!"
        assert without.thumbnail.url is None
        assert without.title != "Badge earned!"


class TestBadgeIcons:
    def test_every_badge_ships_a_png_icon(self) -> None:
        """Each badge's role icon must be packaged with the bot, or recreated roles come back without one."""
        for badge in BADGE_LIST:
            icon = badge_icon(badge.id)
            assert icon is not None, badge.id
            assert icon.startswith(b"\x89PNG"), badge.id
            assert len(icon) < 256 * 1024, badge.id  # Discord's role icon limit

    async def test_new_role_gets_its_icon_when_the_guild_has_role_icons(self) -> None:
        guild = _fake_guild()
        guild.features = [ROLE_ICONS_FEATURE]
        await create_badge_role(guild, BADGE_LIST[0])
        assert guild.create_role.await_args.kwargs["display_icon"] == badge_icon(BADGE_LIST[0].id)

    async def test_no_icon_without_the_boost_feature(self) -> None:
        guild = _fake_guild()
        guild.features = []
        await create_badge_role(guild, BADGE_LIST[0])
        assert guild.create_role.await_args.kwargs["display_icon"] is discord.utils.MISSING

    async def test_existing_role_without_an_icon_gets_one_and_iconned_roles_are_left_alone(self) -> None:
        existing = {badge.id: 1000 + i for i, badge in enumerate(BADGE_LIST)}
        roles = [_fake_role(role_id) for role_id in existing.values()]
        for role in roles:
            role.icon = None
            role.edit = AsyncMock()
        roles[1].icon = MagicMock()
        guild = _fake_guild(roles=roles)
        guild.features = [ROLE_ICONS_FEATURE]
        cog = _make_cog(configs=AsyncMock(), guilds=[guild])

        await cog._ensure_badge_roles(guild, _config(badge_role_ids=existing))

        assert roles[0].edit.await_args.kwargs["display_icon"] == badge_icon(BADGE_LIST[0].id)
        roles[1].edit.assert_not_awaited()
        guild.create_role.assert_not_awaited()


class TestCreateBadgeRole:
    async def test_creates_role_not_hoisted_not_mentionable_with_badge_color(self) -> None:
        guild = _fake_guild()
        badge = BADGE_LIST[0]

        await create_badge_role(guild, badge)

        guild.create_role.assert_awaited_once()
        kwargs = guild.create_role.await_args.kwargs
        assert kwargs["name"] == badge.name
        assert kwargs["colour"] == discord.Colour(badge.color)
        assert kwargs["hoist"] is False
        assert kwargs["mentionable"] is False

    async def test_never_calls_edit_role_positions(self) -> None:
        """Positioning relies on Discord's default placement for new roles; no other role is ever touched."""
        guild = _fake_guild()
        await create_badge_role(guild, BADGE_LIST[0])
        guild.edit_role_positions.assert_not_called()

    async def test_returns_none_and_warns_on_http_exception(self) -> None:
        guild = _fake_guild()
        guild.create_role.side_effect = discord.HTTPException(MagicMock(status=403), "Missing Permissions")

        result = await create_badge_role(guild, BADGE_LIST[0])

        assert result is None


class TestEnsureBadgeRoles:
    async def test_creates_every_badge_role_once(self) -> None:
        guild = _fake_guild()
        created_roles = [_fake_role(1000 + i) for i in range(len(BADGE_LIST))]
        guild.create_role.side_effect = created_roles
        configs = AsyncMock()
        cog = _make_cog(configs=configs, guilds=[guild])

        result = await cog._ensure_badge_roles(guild, _config())

        assert guild.create_role.await_count == len(BADGE_LIST)
        assert set(result) == {badge.id for badge in BADGE_LIST}
        configs.update.assert_awaited_once()
        updated_change = configs.update.await_args.args[1]
        assert set(updated_change.badge_role_ids) == {badge.id for badge in BADGE_LIST}

    async def test_does_not_recreate_roles_that_already_exist(self) -> None:
        existing = {badge.id: 1000 + i for i, badge in enumerate(BADGE_LIST)}
        roles = [_fake_role(role_id) for role_id in existing.values()]
        guild = _fake_guild(roles=roles)
        configs = AsyncMock()
        cog = _make_cog(configs=configs, guilds=[guild])

        result = await cog._ensure_badge_roles(guild, _config(badge_role_ids=existing))

        guild.create_role.assert_not_awaited()
        configs.update.assert_not_awaited()
        assert result == existing

    async def test_recreates_a_role_that_was_deleted_in_discord(self) -> None:
        stored = {"first_steps": 500}
        guild = _fake_guild(roles=[])  # 500 no longer resolves: deleted
        new_role = _fake_role(777)
        guild.create_role.side_effect = [new_role, *[_fake_role(2000 + i) for i in range(len(BADGE_LIST) - 1)]]
        configs = AsyncMock()
        cog = _make_cog(configs=configs, guilds=[guild])

        result = await cog._ensure_badge_roles(guild, _config(badge_role_ids=stored))

        assert result["first_steps"] == 777
        configs.update.assert_awaited_once()


class TestApplyRoleState:
    async def test_adds_and_removes_badge_roles_to_match_role_state(self) -> None:
        keep_role = _fake_role(100)
        drop_role = _fake_role(200)
        guild = _fake_guild(roles=[keep_role, drop_role])
        member = _fake_member(1, role_ids=[200])
        guild.get_member.return_value = member
        cog = _make_cog(guilds=[guild])
        badge_role_ids = {"first_steps": 100, "raid_regular": 200}
        role_state = [BadgeRoleState(discord_id=1, badge_ids=["first_steps"])]

        await cog._apply_role_state(guild, badge_role_ids, role_state)

        member.add_roles.assert_any_await(keep_role, reason="Earned First Steps on sidequest-guild.com")
        member.remove_roles.assert_any_await(drop_role, reason="No longer holds this Hall of Heroes badge")

    async def test_skips_members_not_currently_in_the_guild(self) -> None:
        guild = _fake_guild()
        guild.get_member.return_value = None
        cog = _make_cog(guilds=[guild])

        role_state = [BadgeRoleState(discord_id=1, badge_ids=["first_steps"])]
        await cog._apply_role_state(guild, {"first_steps": 100}, role_state)

        # No exception, no role calls possible since no member was resolved.
        guild.get_role.assert_not_called()

    async def test_top_helper_handoff_adds_for_new_winner_and_removes_from_old(self) -> None:
        top_helper_role = _fake_role(300)
        guild = _fake_guild(roles=[top_helper_role])
        old_winner = _fake_member(1, role_ids=[300])
        new_winner = _fake_member(2, role_ids=[])
        guild.get_member.side_effect = {1: old_winner, 2: new_winner}.get
        cog = _make_cog(guilds=[guild])
        badge_role_ids = {"top_helper": 300}
        role_state = [
            BadgeRoleState(discord_id=1, badge_ids=[]),
            BadgeRoleState(discord_id=2, badge_ids=["top_helper"]),
        ]

        await cog._apply_role_state(guild, badge_role_ids, role_state)

        old_winner.remove_roles.assert_any_await(top_helper_role, reason="No longer holds this Hall of Heroes badge")
        new_winner.add_roles.assert_any_await(top_helper_role, reason="Earned Top Helper on sidequest-guild.com")
        old_winner.add_roles.assert_not_awaited()
        new_winner.remove_roles.assert_not_awaited()

    async def test_role_with_administrator_permission_is_never_granted(self) -> None:
        """If a badge role later gains Administrator, the grant guard must block it (same as away.py)."""
        unsafe_role = _fake_role(100, permissions=discord.Permissions(administrator=True))
        guild = _fake_guild(roles=[unsafe_role])
        member = _fake_member(1, role_ids=[])
        guild.get_member.return_value = member
        cog = _make_cog(guilds=[guild])
        badge_role_ids = {"first_steps": 100}
        role_state = [BadgeRoleState(discord_id=1, badge_ids=["first_steps"])]

        await cog._apply_role_state(guild, badge_role_ids, role_state)

        member.add_roles.assert_not_awaited()


class TestAnnouncePending:
    async def test_posts_embed_with_allowed_mentions_scoped_to_the_earner_then_acks(self) -> None:
        channel = _fake_channel()
        guild = _fake_guild()
        member = _fake_member(1)
        guild.get_member.return_value = member
        guild.get_channel.return_value = channel
        api = AsyncMock()
        cog = _make_cog(api=api, guilds=[guild])

        announced = await cog._announce_pending(guild, _config(badge_channel_id=900), [_announcement()])

        assert announced == 1
        channel.send.assert_awaited_once()
        send_kwargs = channel.send.await_args.kwargs
        allowed_mentions = send_kwargs["allowed_mentions"]
        assert allowed_mentions.users == [member]
        assert [f.filename for f in send_kwargs["files"]] == [BADGE_ICON_FILENAME]
        api.ack_badge.assert_awaited_once_with(guild.id, BadgeAck(discord_id=1, badge_id="raid_regular"))

    async def test_no_channel_configured_skips_posting_and_does_not_ack(self) -> None:
        guild = _fake_guild()
        api = AsyncMock()
        cog = _make_cog(api=api, guilds=[guild])

        announced = await cog._announce_pending(guild, _config(badge_channel_id=None), [_announcement()])

        assert announced == 0
        api.ack_badge.assert_not_awaited()

    async def test_configured_channel_id_not_resolving_to_a_text_channel_skips_without_acking(self) -> None:
        guild = _fake_guild()
        guild.get_channel.return_value = None
        api = AsyncMock()
        cog = _make_cog(api=api, guilds=[guild])

        announced = await cog._announce_pending(guild, _config(badge_channel_id=900), [_announcement()])

        assert announced == 0
        api.ack_badge.assert_not_awaited()

    async def test_ack_failure_still_counts_the_post_as_announced(self) -> None:
        channel = _fake_channel()
        guild = _fake_guild()
        guild.get_channel.return_value = channel
        api = AsyncMock()
        api.ack_badge.side_effect = APIError(500, "boom")
        cog = _make_cog(api=api, guilds=[guild])

        announced = await cog._announce_pending(guild, _config(badge_channel_id=900), [_announcement()])

        assert announced == 1

    async def test_send_failure_is_not_acked(self) -> None:
        channel = _fake_channel()
        channel.send.side_effect = discord.HTTPException(MagicMock(status=500), "boom")
        guild = _fake_guild()
        guild.get_channel.return_value = channel
        api = AsyncMock()
        cog = _make_cog(api=api, guilds=[guild])

        announced = await cog._announce_pending(guild, _config(badge_channel_id=900), [_announcement()])

        assert announced == 0
        api.ack_badge.assert_not_awaited()


class TestSyncLoop:
    async def test_not_found_retries_next_tick_and_recovers(self) -> None:
        """A 404 from the new badges/sync endpoint (rolling deploy) must not permanently disable the cog."""
        guild = _fake_guild()
        api = AsyncMock()
        api.sync_badges.side_effect = [
            NotFoundError(404, "not deployed yet"),
            BadgeSyncReport(role_state=[], pending=[]),
        ]
        configs = AsyncMock()
        configs.get.return_value = _config()
        cog = _make_cog(api=api, configs=configs, guilds=[guild])

        await cog._sync_loop.coro(cog)
        await cog._sync_loop.coro(cog)

        assert api.sync_badges.await_count == 2

    async def test_api_error_does_not_crash_the_loop(self) -> None:
        guild = _fake_guild()
        api = AsyncMock()
        api.sync_badges.side_effect = APIError(500, "boom")
        configs = AsyncMock()
        configs.get.return_value = _config()
        cog = _make_cog(api=api, configs=configs, guilds=[guild])

        await cog._sync_loop.coro(cog)  # must not raise

    async def test_full_sync_creates_roles_applies_state_and_announces(self) -> None:
        channel = _fake_channel()
        badge_role_ids = {badge.id: 1000 + i for i, badge in enumerate(BADGE_LIST)}
        badge_role_ids["first_steps"] = 100
        roles = [_fake_role(role_id) for role_id in set(badge_role_ids.values())]
        first_steps_role = next(role for role in roles if role.id == 100)
        guild = _fake_guild(roles=roles)
        member = _fake_member(1, role_ids=[])
        guild.get_member.return_value = member
        guild.get_channel.return_value = channel
        api = AsyncMock()
        api.sync_badges.return_value = BadgeSyncReport(
            role_state=[BadgeRoleState(discord_id=1, badge_ids=["first_steps"])],
            pending=[_announcement(badge_id="first_steps", name="First Steps", description="Attended an event.")],
        )
        configs = AsyncMock()
        configs.get.return_value = _config(badge_channel_id=900, badge_role_ids=badge_role_ids)
        cog = _make_cog(api=api, configs=configs, guilds=[guild])

        await cog._sync_loop.coro(cog)

        guild.create_role.assert_not_awaited()
        member.add_roles.assert_any_await(first_steps_role, reason="Earned First Steps on sidequest-guild.com")
        channel.send.assert_awaited_once()
        api.ack_badge.assert_awaited_once()


class TestBadgesSyncCommand:
    async def test_reports_member_and_announcement_counts(self) -> None:
        guild = Mock(spec=discord.Guild)
        guild.id = 42
        interaction = Mock(spec=discord.Interaction)
        interaction.guild = guild
        interaction.response = Mock()
        interaction.response.defer = AsyncMock()
        interaction.followup = Mock()
        interaction.followup.send = AsyncMock()
        cog = _make_cog()
        cog._sync_guild = AsyncMock(return_value=(3, 2))

        await cog.badges_sync.callback(cog, interaction)

        interaction.followup.send.assert_awaited_once()
        message = interaction.followup.send.await_args.args[0]
        assert "3" in message
        assert "2" in message

    async def test_not_found_is_reported_as_a_transient_failure(self) -> None:
        """NotFoundError has no reliable "wrong guild" signal here, so it's treated like any other APIError."""
        guild = Mock(spec=discord.Guild)
        guild.id = 42
        interaction = Mock(spec=discord.Interaction)
        interaction.guild = guild
        interaction.response = Mock()
        interaction.response.defer = AsyncMock()
        interaction.followup = Mock()
        interaction.followup.send = AsyncMock()
        cog = _make_cog()
        cog._sync_guild = AsyncMock(side_effect=NotFoundError(404, "not deployed yet"))

        await cog.badges_sync.callback(cog, interaction)

        message = interaction.followup.send.await_args.args[0]
        assert "try again shortly" in message


class TestBadgesChannelCommand:
    async def test_sets_channel(self) -> None:
        guild = Mock(spec=discord.Guild)
        guild.id = 42
        channel = Mock(spec=discord.TextChannel)
        channel.id = 900
        channel.mention = "#badges"
        interaction = Mock(spec=discord.Interaction)
        interaction.guild = guild
        interaction.response = Mock()
        interaction.response.send_message = AsyncMock()
        interaction.client = Mock()
        interaction.client.configs = Mock()
        interaction.client.configs.update = AsyncMock()
        cog = _make_cog()

        await cog.badges_channel.callback(cog, interaction, channel)

        update_call = interaction.client.configs.update.await_args
        assert update_call.args[0] == 42
        assert update_call.args[1].badge_channel_id == 900
        interaction.response.send_message.assert_awaited_once()

    async def test_clears_channel(self) -> None:
        guild = Mock(spec=discord.Guild)
        guild.id = 42
        interaction = Mock(spec=discord.Interaction)
        interaction.guild = guild
        interaction.response = Mock()
        interaction.response.send_message = AsyncMock()
        interaction.client = Mock()
        interaction.client.configs = Mock()
        interaction.client.configs.update = AsyncMock()
        cog = _make_cog()

        await cog.badges_channel.callback(cog, interaction, None)

        update_call = interaction.client.configs.update.await_args
        assert update_call.args[1].badge_channel_id is None
