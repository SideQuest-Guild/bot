"""Round trips of ``api_client``'s group-voice (LFG) methods against the real, in-process API.

Uses ``conftest.py``'s ``api``/``client`` fixtures (the real Litestar app on a fresh SQLite file), not respx mocks:
these exercise the actual wire contract between the bot and the website, not a hand-maintained guess at its shape.
"""

from uuid import uuid4

import pytest

from sidequest_bot.api_client import APIError, NotFoundError, SideQuestAPI
from sidequest_common.schemas import (
    EventKind,
    GroupEndReason,
    GroupMemberMinutes,
    GroupPairMinutes,
    GroupPresenceReport,
    GroupSessionClose,
    GroupSessionCreate,
    GroupSessionUpdate,
    SignupRole,
)
from sidequest_common.settings import WebSettings

GUILD_ID = WebSettings(_env_file=None).GUILD_ID
OTHER_GUILD_ID = GUILD_ID + 1


def _create(**overrides: object) -> GroupSessionCreate:
    base: dict[str, object] = {
        "discord_channel_id": "111",
        "channel_name": "dungeon-1",
        "kind": EventKind.DUNGEON,
        "creator_discord_id": "222",
    }
    base.update(overrides)
    return GroupSessionCreate.model_validate(base)


async def test_create_group_returns_the_session(api: SideQuestAPI) -> None:
    session = await api.create_group(GUILD_ID, _create())
    assert session.discord_channel_id == "111"
    assert session.channel_name == "dungeon-1"
    assert session.kind == EventKind.DUNGEON
    assert session.creator_discord_id == "222"
    assert session.owner_discord_id == "222"
    assert session.ended_at is None
    assert session.members == []
    assert session.pairs == []


async def test_create_group_for_the_wrong_guild_404s(api: SideQuestAPI) -> None:
    with pytest.raises(NotFoundError):
        await api.create_group(OTHER_GUILD_ID, _create())


async def test_create_group_twice_for_the_same_channel_conflicts(api: SideQuestAPI) -> None:
    await api.create_group(GUILD_ID, _create())
    with pytest.raises(APIError) as excinfo:
        await api.create_group(GUILD_ID, _create())
    assert excinfo.value.status_code == 409


async def test_get_group_returns_the_session(api: SideQuestAPI) -> None:
    created = await api.create_group(GUILD_ID, _create())
    fetched = await api.get_group(GUILD_ID, created.id)
    assert fetched.id == created.id


async def test_get_group_unknown_session_404s(api: SideQuestAPI) -> None:
    with pytest.raises(NotFoundError):
        await api.get_group(GUILD_ID, uuid4())


async def test_list_open_groups_returns_sessions_without_an_end(api: SideQuestAPI) -> None:
    created = await api.create_group(GUILD_ID, _create())
    open_sessions = await api.list_open_groups(GUILD_ID)
    assert [session.id for session in open_sessions] == [created.id]


async def test_list_open_groups_excludes_closed_sessions(api: SideQuestAPI) -> None:
    created = await api.create_group(GUILD_ID, _create())
    await api.close_group(
        GUILD_ID,
        created.id,
        GroupSessionClose(
            end_reason=GroupEndReason.OWNER, presence=GroupPresenceReport(peak_size=0, grouped_minutes=0)
        ),
    )
    assert await api.list_open_groups(GUILD_ID) == []


async def test_update_group_changes_only_sent_fields(api: SideQuestAPI) -> None:
    created = await api.create_group(GUILD_ID, _create())
    updated = await api.update_group(
        GUILD_ID, created.id, GroupSessionUpdate(name="Deadmines", needs=[SignupRole.HEALER])
    )
    assert updated.name == "Deadmines"
    assert updated.needs == [SignupRole.HEALER]
    assert updated.channel_name == "dungeon-1"  # untouched


async def test_update_group_channel_name_null_is_ignored(api: SideQuestAPI) -> None:
    created = await api.create_group(GUILD_ID, _create())
    renamed = await api.update_group(GUILD_ID, created.id, GroupSessionUpdate(channel_name="dungeon-deadmines"))
    assert renamed.channel_name == "dungeon-deadmines"
    untouched = await api.update_group(GUILD_ID, created.id, GroupSessionUpdate(channel_name=None, cap=5))
    assert untouched.channel_name == "dungeon-deadmines"
    assert untouched.cap == 5


async def test_report_group_presence_raises_stored_totals_to_the_max(api: SideQuestAPI) -> None:
    created = await api.create_group(GUILD_ID, _create())
    await api.report_group_presence(
        GUILD_ID,
        created.id,
        GroupPresenceReport(
            peak_size=2,
            grouped_minutes=5,
            members=[GroupMemberMinutes(discord_id="1", display_name="Alice", minutes=5)],
            pairs=[GroupPairMinutes(discord_id_a="1", discord_id_b="2", minutes=5)],
        ),
    )
    await api.report_group_presence(
        GUILD_ID,
        created.id,
        GroupPresenceReport(
            peak_size=1,
            grouped_minutes=3,
            members=[GroupMemberMinutes(discord_id="1", display_name="Alice", minutes=3)],
            pairs=[],
        ),
    )
    fetched = await api.get_group(GUILD_ID, created.id)
    assert fetched.peak_size == 2
    assert fetched.grouped_minutes == 5
    assert {member.discord_id: member.minutes for member in fetched.members} == {"1": 5}
    assert {(pair.discord_id_a, pair.discord_id_b): pair.minutes for pair in fetched.pairs} == {("1", "2"): 5}


async def test_close_group_ends_the_session(api: SideQuestAPI) -> None:
    created = await api.create_group(GUILD_ID, _create())
    await api.close_group(
        GUILD_ID,
        created.id,
        GroupSessionClose(
            end_reason=GroupEndReason.EMPTY, presence=GroupPresenceReport(peak_size=0, grouped_minutes=0)
        ),
    )
    fetched = await api.get_group(GUILD_ID, created.id)
    assert fetched.ended_at is not None
    assert fetched.end_reason == GroupEndReason.EMPTY


async def test_close_group_twice_keeps_the_first_reason(api: SideQuestAPI) -> None:
    created = await api.create_group(GUILD_ID, _create())
    await api.close_group(
        GUILD_ID,
        created.id,
        GroupSessionClose(
            end_reason=GroupEndReason.OWNER, presence=GroupPresenceReport(peak_size=0, grouped_minutes=0)
        ),
    )
    await api.close_group(
        GUILD_ID,
        created.id,
        GroupSessionClose(
            end_reason=GroupEndReason.LOST,
            presence=GroupPresenceReport(
                peak_size=1,
                grouped_minutes=0,
                members=[GroupMemberMinutes(discord_id="1", display_name="Alice", minutes=1)],
            ),
        ),
    )
    fetched = await api.get_group(GUILD_ID, created.id)
    assert fetched.end_reason == GroupEndReason.OWNER
    assert {member.discord_id: member.minutes for member in fetched.members} == {"1": 1}


async def test_purge_group_transcripts_returns_a_count(api: SideQuestAPI) -> None:
    report = await api.purge_group_transcripts(GUILD_ID)
    assert report.deleted == 0
