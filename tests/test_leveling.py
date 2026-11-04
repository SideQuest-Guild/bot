from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from sidequest_bot.api_client import SideQuestAPI
from sidequest_bot.plugins import leveling as leveling_module
from sidequest_bot.plugins.leveling import is_xp_eligible, render_progress_bar
from sidequest_common.schemas import VOICE_XP_PER_MINUTE, GuildConfigSchema, XPActivityReport, XPGrantResult

_GUILD_SENTINEL = object()


def _make_config(**overrides: Any) -> GuildConfigSchema:
    base: dict[str, Any] = {
        "guild_id": 1,
        "log_channel_id": None,
        "welcome_channel_id": None,
        "welcome_message": "hi",
        "auto_role_id": None,
        "linked_role_id": None,
        "battlenet_role_id": None,
        "leveling_enabled": True,
        "level_up_channel_id": None,
        "level_up_message": "GG {mention}, level {level}!",
        "xp_min": 15,
        "xp_max": 25,
        "xp_cooldown_seconds": 60,
        "level_roles_stack": True,
        "no_xp_channel_ids": [],
        "no_xp_role_ids": [],
        "automod_enabled": True,
        "automod_block_invites": True,
        "automod_block_links": False,
        "automod_max_mentions": 5,
        "automod_spam_messages": 5,
        "automod_spam_seconds": 5,
        "automod_timeout_seconds": 300,
        "automod_exempt_role_ids": [],
        "automod_exempt_channel_ids": [],
    }
    base.update(overrides)
    return GuildConfigSchema(**base)


def _make_message(
    *,
    author_bot: bool = False,
    webhook_id: int | None = None,
    guild: Any | None = _GUILD_SENTINEL,
    system: bool = False,
    channel_id: int = 10,
    parent_id: int | None = None,
    author_role_ids: tuple[int, ...] = (),
) -> SimpleNamespace:
    author = SimpleNamespace(bot=author_bot, roles=[SimpleNamespace(id=rid) for rid in author_role_ids])
    channel: Any = SimpleNamespace(id=channel_id)
    if parent_id is not None:
        channel.parent_id = parent_id
    resolved_guild = SimpleNamespace(id=1) if guild is _GUILD_SENTINEL else guild
    return SimpleNamespace(
        author=author, webhook_id=webhook_id, guild=resolved_guild, channel=channel, is_system=lambda: system
    )


# --- is_xp_eligible


def test_bot_message_not_eligible() -> None:
    assert is_xp_eligible(_make_message(author_bot=True), _make_config()) is False


def test_webhook_message_not_eligible() -> None:
    assert is_xp_eligible(_make_message(webhook_id=555), _make_config()) is False


def test_dm_message_not_eligible() -> None:
    assert is_xp_eligible(_make_message(guild=None), _make_config()) is False


def test_system_message_not_eligible() -> None:
    assert is_xp_eligible(_make_message(system=True), _make_config()) is False


def test_leveling_disabled_not_eligible() -> None:
    assert is_xp_eligible(_make_message(), _make_config(leveling_enabled=False)) is False


def test_no_xp_channel_not_eligible() -> None:
    message = _make_message(channel_id=42)
    assert is_xp_eligible(message, _make_config(no_xp_channel_ids=[42])) is False


def test_no_xp_thread_parent_channel_not_eligible() -> None:
    message = _make_message(channel_id=99, parent_id=42)
    assert is_xp_eligible(message, _make_config(no_xp_channel_ids=[42])) is False


def test_no_xp_role_not_eligible() -> None:
    message = _make_message(author_role_ids=(7, 8))
    assert is_xp_eligible(message, _make_config(no_xp_role_ids=[8])) is False


def test_normal_message_eligible() -> None:
    message = _make_message(channel_id=1, author_role_ids=(1, 2))
    assert is_xp_eligible(message, _make_config(no_xp_channel_ids=[99], no_xp_role_ids=[5])) is True


# --- render_progress_bar


def test_progress_bar_empty_at_zero() -> None:
    assert render_progress_bar(0, 100, length=10) == "░" * 10


def test_progress_bar_full_at_total() -> None:
    assert render_progress_bar(100, 100, length=10) == "█" * 10


def test_progress_bar_half_filled() -> None:
    assert render_progress_bar(50, 100, length=10) == "█" * 5 + "░" * 5


def test_progress_bar_clamps_overflow() -> None:
    assert render_progress_bar(500, 100, length=10) == "█" * 10


def test_progress_bar_zero_total_is_full() -> None:
    assert render_progress_bar(0, 0, length=10) == "█" * 10


# --- reward_set / _apply_role_deltas privilege checks


def _make_interaction(*, guild_id: int = 1) -> MagicMock:
    interaction = MagicMock()
    interaction.guild_id = guild_id
    interaction.user = MagicMock(spec=discord.Member)
    interaction.response.send_message = AsyncMock()
    interaction.client.api.set_level_role = AsyncMock()
    return interaction


async def test_reward_set_blocked_when_member_cannot_configure_role() -> None:
    interaction = _make_interaction()
    role = MagicMock(spec=discord.Role)
    with (
        patch.object(leveling_module, "configure_block_reason", return_value="You can't configure that role."),
        patch.object(leveling_module, "grant_block_reason", return_value=None) as grant_mock,
    ):
        await leveling_module.reward_set.callback(interaction, 5, role)
    interaction.response.send_message.assert_awaited_once_with("You can't configure that role.", ephemeral=True)
    interaction.client.api.set_level_role.assert_not_called()
    grant_mock.assert_not_called()  # fails fast on the configure check; never reaches the grant check


async def test_reward_set_blocked_when_role_itself_is_dangerous() -> None:
    """A Manage Server member can't configure an Administrator role as a reward, even if they outrank it."""
    interaction = _make_interaction()
    role = MagicMock(spec=discord.Role)
    with (
        patch.object(leveling_module, "configure_block_reason", return_value=None),
        patch.object(leveling_module, "grant_block_reason", return_value="has admin permissions"),
    ):
        await leveling_module.reward_set.callback(interaction, 5, role)
    interaction.response.send_message.assert_awaited_once_with("has admin permissions", ephemeral=True)
    interaction.client.api.set_level_role.assert_not_called()


async def test_reward_set_allowed_when_both_checks_pass() -> None:
    interaction = _make_interaction()
    role = MagicMock(spec=discord.Role)
    role.id = 999
    role.mention = "<@&999>"
    with (
        patch.object(leveling_module, "configure_block_reason", return_value=None),
        patch.object(leveling_module, "grant_block_reason", return_value=None),
    ):
        await leveling_module.reward_set.callback(interaction, 5, role)
    interaction.client.api.set_level_role.assert_awaited_once_with(1, 5, 999)


async def test_apply_role_deltas_rechecks_grant_block_reason_per_role() -> None:
    """A role that became dangerous after being configured as a reward must not be granted."""
    cog = leveling_module.LevelingCog.__new__(leveling_module.LevelingCog)

    safe_role = MagicMock(spec=discord.Role)
    safe_role.id = 1
    now_dangerous_role = MagicMock(spec=discord.Role)
    now_dangerous_role.id = 2

    guild = MagicMock(spec=discord.Guild)
    guild.id = 42
    guild.get_role.side_effect = {1: safe_role, 2: now_dangerous_role}.get

    member = MagicMock(spec=discord.Member)
    member.add_roles = AsyncMock()
    member.remove_roles = AsyncMock()

    def block_reason(role: MagicMock) -> str | None:
        return None if role is safe_role else "has admin permissions"

    with patch.object(leveling_module, "grant_block_reason", side_effect=block_reason):
        await cog._apply_role_deltas(guild, member, [1, 2], [])

    member.add_roles.assert_awaited_once_with(safe_role, reason="Level-up reward")


# --- voice and activity XP


def _voice_member(
    member_id: int, *, deaf: bool = False, bot: bool = False, role_ids: tuple[int, ...] = ()
) -> MagicMock:
    member = MagicMock(spec=discord.Member)
    member.id = member_id
    member.bot = bot
    member.mention = f"<@{member_id}>"
    member.display_name = f"m{member_id}"
    member.roles = [SimpleNamespace(id=role_id) for role_id in role_ids]
    member.voice = SimpleNamespace(self_deaf=deaf, deaf=False)
    member.add_roles = AsyncMock()
    member.remove_roles = AsyncMock()
    return member


def _voice_channel(channel_id: int, members: list[MagicMock], *, category_id: int | None = None) -> MagicMock:
    channel = MagicMock(spec=discord.VoiceChannel)
    channel.id = channel_id
    channel.category_id = category_id
    channel.members = members
    channel.send = AsyncMock()
    return channel


def _voice_guild(*channels: MagicMock, afk: MagicMock | None = None) -> MagicMock:
    guild = MagicMock(spec=discord.Guild)
    guild.id = 1564671275316543508
    guild.name = "SideQuest"
    guild.voice_channels = list(channels)
    guild.stage_channels = []
    guild.afk_channel = afk
    guild.system_channel = None
    members = {member.id: member for channel in channels for member in channel.members}
    guild.get_member.side_effect = members.get
    guild.get_role.return_value = None
    return guild


class TestVoiceXPEarners:
    def test_only_people_with_company_who_can_hear_earn(self) -> None:
        alone = _voice_channel(10, [_voice_member(1)])
        pair = _voice_channel(11, [_voice_member(2), _voice_member(3), _voice_member(4, deaf=True)])
        with_bot = _voice_channel(12, [_voice_member(5), _voice_member(6, bot=True)])

        earners = leveling_module.voice_xp_earners(_voice_guild(alone, pair, with_bot), _make_config())

        assert sorted(earners) == [2, 3]
        assert earners[2] is pair

    def test_afk_no_xp_channels_categories_and_roles_earn_nothing(self) -> None:
        afk = _voice_channel(20, [_voice_member(1), _voice_member(2)])
        muted_channel = _voice_channel(21, [_voice_member(3), _voice_member(4)])
        muted_category = _voice_channel(22, [_voice_member(5), _voice_member(6)], category_id=99)
        open_channel = _voice_channel(23, [_voice_member(7, role_ids=(500,)), _voice_member(8)])
        config = _make_config(no_xp_channel_ids=[21, 99], no_xp_role_ids=[500])

        earners = leveling_module.voice_xp_earners(
            _voice_guild(afk, muted_channel, muted_category, open_channel, afk=afk), config
        )

        assert list(earners) == [8]

    def test_nobody_earns_while_leveling_is_off(self) -> None:
        pair = _voice_channel(30, [_voice_member(1), _voice_member(2)])
        assert leveling_module.voice_xp_earners(_voice_guild(pair), _make_config(leveling_enabled=False)) == {}


def _grant(user_id: int, *, source: str, amount: int, xp: int, level: int, previous_level: int, **roles: list[int]):
    return XPGrantResult(
        awarded=True,
        amount=amount,
        xp=xp,
        level=level,
        previous_level=previous_level,
        leveled_up=level > previous_level,
        user_id=user_id,
        source=source,
        earned_role_ids=roles.get("earned", []),
        remove_role_ids=roles.get("removed", []),
    )


def test_two_grants_in_one_sync_announce_one_level_up_with_every_role_change() -> None:
    merged = leveling_module.merge_grants(
        [
            _grant(1, source="event", amount=150, xp=150, level=1, previous_level=0, earned=[10]),
            _grant(1, source="group", amount=50, xp=200, level=1, previous_level=1),
            _grant(2, source="group", amount=50, xp=50, level=0, previous_level=0),
        ]
    )

    assert merged[1].leveled_up is True
    assert (merged[1].previous_level, merged[1].level, merged[1].amount) == (0, 1, 200)
    assert merged[1].earned_role_ids == [10]
    assert merged[2].leveled_up is False


def _cog_with(api: Any, config: GuildConfigSchema) -> leveling_module.LevelingCog:
    cog = leveling_module.LevelingCog.__new__(leveling_module.LevelingCog)
    configs = AsyncMock()
    configs.get.return_value = config
    cog.bot = SimpleNamespace(api=api, configs=configs)
    return cog


async def test_a_minute_in_voice_earns_xp_and_announces_level_ups_in_the_voice_chat(api: SideQuestAPI) -> None:
    near, far = _voice_member(41), _voice_member(42)
    channel = _voice_channel(40, [near, far])
    guild = _voice_guild(channel)
    await api.set_xp(guild.id, near.id, 100 - VOICE_XP_PER_MINUTE)
    cog = _cog_with(api, _make_config(guild_id=guild.id))

    await cog.grant_voice_xp(guild)

    assert (await api.get_rank(guild.id, far.id)).xp == VOICE_XP_PER_MINUTE
    assert (await api.get_rank(guild.id, near.id)).level == 1
    channel.send.assert_awaited_once()
    assert channel.send.await_args.args[0] == "GG <@41>, level 1!"


async def test_activity_sync_pays_out_and_announces_in_the_system_channel(api: SideQuestAPI) -> None:
    guild = _voice_guild()
    guild.system_channel = MagicMock(spec=discord.TextChannel)
    guild.system_channel.send = AsyncMock()
    cog = _cog_with(api, _make_config(guild_id=guild.id))
    report = XPActivityReport(
        grants=[_grant(7, source="event", amount=150, xp=150, level=1, previous_level=0)],
    )
    member = _voice_member(7)
    guild.get_member.side_effect = {7: member}.get

    with patch.object(api, "sync_activity_xp", AsyncMock(return_value=report)):
        await cog.sync_activity_xp(guild)

    guild.system_channel.send.assert_awaited_once()
    assert (await api.sync_activity_xp(guild.id)).grants == []  # the real route answers, with nothing new to pay
