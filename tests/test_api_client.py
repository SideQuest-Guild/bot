import json
from uuid import UUID, uuid4

import httpx
import pytest
import respx
from tenacity import wait_none

from sidequest_bot import api_client
from sidequest_bot.api_client import (
    APIError,
    APIResponseShapeError,
    APIUnavailableError,
    NotFoundError,
    SideQuestAPI,
)
from sidequest_common.schemas import (
    CaseAction,
    ConfirmByDiscordRequest,
    DiscordEventReconcile,
    DiscordEventState,
    DiscordEventStatus,
    DiscordLiveReport,
    DiscordPushAck,
    GuildRoleIn,
    MemberLink,
    MemberProfileIn,
    MessageBackfill,
    MessageBackfillBucket,
    ModCaseCreate,
    SignupNoticeAck,
)

BASE = "http://api.test"
CONFIG_URL = f"{BASE}/api/guilds/1/config"
EVENT_SYNC_URL = f"{BASE}/api/guilds/1/event-sync"
CASES_URL = f"{BASE}/api/guilds/1/cases"
MEMBER_LINKS_URL = f"{BASE}/api/guilds/1/member-links"
MEMBER_PROFILES_URL = f"{BASE}/api/guilds/1/member-profiles"
AWAY_NOW_URL = f"{BASE}/api/guilds/1/away-now"


def _event_state(**overrides: object) -> DiscordEventState:
    base: dict[str, object] = {
        "id": 1,
        "name": "Raid Night",
        "description": "desc",
        "starts_at": "2026-01-01T00:00:00+00:00",
        "ends_at": "2026-01-01T03:00:00+00:00",
        "status": DiscordEventStatus.SCHEDULED,
        "location": "Molten Core",
        "channel_id": None,
    }
    base.update(overrides)
    return DiscordEventState.model_validate(base)


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api_client, "wait_exponential", lambda **_: wait_none())


@pytest.fixture
async def api() -> SideQuestAPI:
    return SideQuestAPI(BASE, "key")


def _case() -> ModCaseCreate:
    return ModCaseCreate(action=CaseAction.WARN, user_id=1, moderator_id=2)


@respx.mock
async def test_get_retries_server_errors_then_succeeds(api: SideQuestAPI) -> None:
    route = respx.get(CONFIG_URL).mock(
        side_effect=[httpx.Response(503, json={"detail": "busy"}), httpx.Response(500), httpx.Response(404)]
    )
    with pytest.raises(NotFoundError):
        await api.get_guild_config(1)
    assert route.call_count == 3


@respx.mock
async def test_post_is_not_retried_after_reaching_the_server(api: SideQuestAPI) -> None:
    """A 5xx on POST may have committed: retrying could record the same case twice."""
    route = respx.post(CASES_URL).mock(return_value=httpx.Response(500, json={"detail": "boom"}))
    with pytest.raises(APIError) as excinfo:
        await api.create_case(1, _case())
    assert route.call_count == 1
    assert excinfo.value.status_code == 500


@respx.mock
async def test_post_retries_when_connection_never_opened(api: SideQuestAPI) -> None:
    route = respx.post(CASES_URL).mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(APIUnavailableError):
        await api.create_case(1, _case())
    assert route.call_count == 3


@respx.mock
async def test_post_read_timeout_surfaces_as_api_error_without_retry(api: SideQuestAPI) -> None:
    route = respx.post(CASES_URL).mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(APIError) as excinfo:
        await api.create_case(1, _case())
    assert isinstance(excinfo.value, APIUnavailableError)
    assert route.call_count == 1


@respx.mock
async def test_client_errors_are_never_retried(api: SideQuestAPI) -> None:
    route = respx.get(CONFIG_URL).mock(return_value=httpx.Response(401, json={"detail": "bad key"}))
    with pytest.raises(APIError) as excinfo:
        await api.get_guild_config(1)
    assert route.call_count == 1
    assert excinfo.value.detail == "bad key"


@respx.mock
@pytest.mark.parametrize(
    ("url", "body", "call"),
    [
        (CONFIG_URL, {}, lambda api: api.get_guild_config(1)),
        (f"{BASE}/api/events", [{"id": "x"}], lambda api: api.list_upcoming_events()),
    ],
    ids=["model", "list"],
)
async def test_unexpected_response_shape_is_an_api_error(
    api: SideQuestAPI, url: str, body: object, call: object
) -> None:
    # The other half of a rolling deploy answers in an older shape. That must reach callers as an APIError (which
    # every bot loop already handles), never as a bare pydantic error that would kill a discord.py tasks.loop.
    respx.get(url).mock(return_value=httpx.Response(200, json=body))

    with pytest.raises(APIError) as excinfo:
        await call(api)  # type: ignore[operator]

    assert isinstance(excinfo.value, APIResponseShapeError)
    assert excinfo.value.status_code == 502


@respx.mock
async def test_get_pending_event_sync_parses_pushes_and_deletions(api: SideQuestAPI) -> None:
    event_id = uuid4()
    respx.get(EVENT_SYNC_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "pushes": [
                    {
                        "event_id": str(event_id),
                        "discord_event_id": None,
                        "kind": "raid",
                        "action": "upsert",
                        "name": "Raid Night",
                        "title": "Raid Night",
                        "description": "desc",
                        "location": "Molten Core",
                        "starts_at": "2026-01-01T00:00:00Z",
                        "ends_at": "2026-01-01T03:00:00Z",
                        "signup_url": "https://example.com/events/detail/?id=abc",
                        "push_hash": "abc",
                    }
                ],
                "deletions": [42],
            },
        )
    )
    pending = await api.get_pending_event_sync(1)
    assert pending.pushes[0].event_id == event_id
    assert pending.deletions == [42]


@respx.mock
async def test_ack_event_push_posts_to_event_route(api: SideQuestAPI) -> None:
    event_id = uuid4()
    route = respx.post(f"{EVENT_SYNC_URL}/{event_id}/ack").mock(return_value=httpx.Response(204))
    await api.ack_event_push(1, event_id, DiscordPushAck(push_hash="abc", event=_event_state()))
    assert route.call_count == 1


@respx.mock
async def test_report_discord_event_posts_state(api: SideQuestAPI) -> None:
    route = respx.post(f"{EVENT_SYNC_URL}/events").mock(return_value=httpx.Response(204))
    await api.report_discord_event(1, _event_state())
    assert route.call_count == 1


@respx.mock
async def test_report_discord_event_deleted_deletes_event(api: SideQuestAPI) -> None:
    route = respx.delete(f"{EVENT_SYNC_URL}/events/42").mock(return_value=httpx.Response(204))
    await api.report_discord_event_deleted(1, 42)
    assert route.call_count == 1


@respx.mock
async def test_delete_event_sync_tombstone_deletes_tombstone(api: SideQuestAPI) -> None:
    route = respx.delete(f"{EVENT_SYNC_URL}/tombstones/42").mock(return_value=httpx.Response(204))
    await api.delete_event_sync_tombstone(1, 42)
    assert route.call_count == 1


@respx.mock
async def test_reconcile_event_sync_posts_events(api: SideQuestAPI) -> None:
    route = respx.post(f"{EVENT_SYNC_URL}/reconcile").mock(return_value=httpx.Response(204))
    await api.reconcile_event_sync(1, DiscordEventReconcile(events=[_event_state()]))
    assert route.call_count == 1


@respx.mock
async def test_report_discord_live_puts_report(api: SideQuestAPI) -> None:
    route = respx.put(f"{BASE}/api/guilds/1/live").mock(return_value=httpx.Response(204))
    await api.report_discord_live(1, DiscordLiveReport(online=5, members=20, voice=[]))
    assert route.call_count == 1


@respx.mock
async def test_list_member_links_parses_response(api: SideQuestAPI) -> None:
    route = respx.get(MEMBER_LINKS_URL).mock(
        return_value=httpx.Response(200, json=[{"discord_id": 123, "battlenet_linked": True}])
    )
    links = await api.list_member_links(1)
    assert route.call_count == 1
    assert links == [MemberLink(discord_id=123, battlenet_linked=True)]


@respx.mock
async def test_get_away_now_parses_response(api: SideQuestAPI) -> None:
    route = respx.get(AWAY_NOW_URL).mock(return_value=httpx.Response(200, json={"discord_ids": [123, 456]}))
    discord_ids = await api.get_away_now(1)
    assert route.call_count == 1
    assert discord_ids == [123, 456]


@respx.mock
async def test_put_guild_roles_puts_full_set(api: SideQuestAPI) -> None:
    route = respx.put(f"{BASE}/api/guilds/1/roles").mock(return_value=httpx.Response(204))
    roles = [GuildRoleIn(id=1, name="Officer", color=16711680, position=2, managed=False)]
    await api.put_guild_roles(1, roles)
    assert route.call_count == 1
    sent = json.loads(route.calls.last.request.content)
    assert sent == {"roles": [{"id": 1, "name": "Officer", "color": 16711680, "position": 2, "managed": False}]}


@respx.mock
async def test_put_member_profiles_puts_profiles_and_departed_ids(api: SideQuestAPI) -> None:
    route = respx.put(MEMBER_PROFILES_URL).mock(return_value=httpx.Response(204))
    profiles = [
        MemberProfileIn(
            discord_id=1,
            username="hailcaesar4288",
            global_name="HailCaesar",
            guild_nick=None,
            avatar_hash="abc123",
            roles=[100],
        )
    ]
    await api.put_member_profiles(1, profiles, [2])
    assert route.call_count == 1
    sent = json.loads(route.calls.last.request.content)
    assert sent == {
        "profiles": [
            {
                "discord_id": 1,
                "username": "hailcaesar4288",
                "global_name": "HailCaesar",
                "guild_nick": None,
                "avatar_hash": "abc123",
                "roles": [100],
            }
        ],
        "departed_ids": [2],
    }


ACTIVITY_MESSAGES_URL = f"{BASE}/api/guilds/1/activity/messages"


@respx.mock
async def test_backfill_messages_puts_buckets_and_parses_result(api: SideQuestAPI) -> None:
    route = respx.put(ACTIVITY_MESSAGES_URL).mock(return_value=httpx.Response(200, json={"buckets": 2}))
    buckets = [
        MessageBackfillBucket(user_id=1, hour_start="2026-01-01T12:00:00+00:00", messages=5),
        MessageBackfillBucket(user_id=2, hour_start="2026-01-01T13:00:00+00:00", messages=3),
    ]

    result = await api.backfill_messages(1, MessageBackfill(buckets=buckets))

    assert route.call_count == 1
    sent = json.loads(route.calls.last.request.content)
    assert sent == {
        "buckets": [
            {"user_id": 1, "hour_start": "2026-01-01T12:00:00Z", "messages": 5},
            {"user_id": 2, "hour_start": "2026-01-01T13:00:00Z", "messages": 3},
        ]
    }
    assert result.buckets == 2


SIGNUP_NOTICES_URL = f"{BASE}/api/guilds/1/signup-notices"


@respx.mock
async def test_list_signup_notices_parses_pending_and_resolved(api: SideQuestAPI) -> None:
    respx.get(SIGNUP_NOTICES_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "pending": [
                    {
                        "member_id": 1,
                        "discord_id": "100",
                        "display_name": "Newbie",
                        "username": "newbie",
                        "avatar_hash": None,
                        "battletag": None,
                        "roster_confirmed": False,
                        "created_at": "2026-01-01T00:00:00Z",
                    }
                ],
                "resolved": [
                    {
                        "member_id": 2,
                        "discord_id": "200",
                        "display_name": "Confirmed",
                        "channel_id": "555",
                        "message_id": "666",
                        "confirmed_by": "Officer",
                    }
                ],
            },
        )
    )
    page = await api.list_signup_notices(1)
    assert [m.member_id for m in page.pending] == [1]
    assert [m.member_id for m in page.resolved] == [2]


@respx.mock
async def test_ack_signup_notice_posts_channel_and_message(api: SideQuestAPI) -> None:
    route = respx.post(f"{SIGNUP_NOTICES_URL}/1/ack").mock(return_value=httpx.Response(204))
    await api.ack_signup_notice(1, 1, SignupNoticeAck(channel_id=555, message_id=666))
    assert route.call_count == 1
    sent = json.loads(route.calls.last.request.content)
    assert sent == {"channel_id": 555, "message_id": 666}


@respx.mock
async def test_confirm_roster_from_notice_puts_actor(api: SideQuestAPI) -> None:
    route = respx.put(f"{SIGNUP_NOTICES_URL}/100/roster-confirmation").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 1,
                "username": "newbie",
                "display_name": "Newbie",
                "avatar_url": None,
                "is_guild_member": True,
                "is_officer": False,
                "battletag": None,
                "roster_confirmed": True,
                "last_login_at": None,
            },
        )
    )
    request = ConfirmByDiscordRequest(actor_discord_id="200", actor_name="GM")
    listing = await api.confirm_roster_from_notice(1, 100, request)
    assert route.call_count == 1
    assert listing.roster_confirmed is True
    sent = json.loads(route.calls.last.request.content)
    assert sent == {"actor_discord_id": "200", "actor_name": "GM"}


ITEMS_URL = f"{BASE}/api/items"
_ITEM_JSON = {
    "item_id": 19019,
    "name": "Thunderfury, Blessed Blade of the Windseeker",
    "quality": "LEGENDARY",
    "quality_name": "Legendary",
    "item_class": "Weapon",
    "item_subclass": "Sword",
    "inventory_type": "ONEHWEAPON",
    "inventory_type_name": "One-Hand",
    "item_level": 80,
    "required_level": 60,
    "sell_price": 50000,
    "is_equippable": True,
    "icon_url": "https://render.worldofwarcraft.com/icons/56/inv_sword_39.jpg",
    "tooltip": ["Binds when picked up", "Unique", "One-Hand Sword"],
}


@respx.mock
async def test_search_items_sends_query_and_parses_page(api: SideQuestAPI) -> None:
    route = respx.get(ITEMS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "page": 1,
                "page_count": 1,
                "results": [
                    {
                        "item_id": 19019,
                        "name": "Thunderfury, Blessed Blade of the Windseeker",
                        "quality": "LEGENDARY",
                        "quality_name": "Legendary",
                        "inventory_type": "ONEHWEAPON",
                        "inventory_type_name": "One-Hand",
                        "item_level": 80,
                        "required_level": 60,
                    }
                ],
            },
        )
    )
    page = await api.search_items("thunderfury")
    assert route.call_count == 1
    assert dict(route.calls.last.request.url.params) == {"q": "thunderfury", "page": "1"}
    assert page.page_count == 1
    assert page.results == [
        api_client.ItemSummary(
            item_id=19019,
            name="Thunderfury, Blessed Blade of the Windseeker",
            quality="LEGENDARY",
            quality_name="Legendary",
            inventory_type="ONEHWEAPON",
            inventory_type_name="One-Hand",
            item_level=80,
            required_level=60,
        )
    ]


@respx.mock
async def test_search_items_sends_quality_and_slot_filters(api: SideQuestAPI) -> None:
    route = respx.get(ITEMS_URL).mock(
        return_value=httpx.Response(200, json={"page": 1, "page_count": 0, "results": []})
    )
    await api.search_items("bless", page=2, qualities=("EPIC", "LEGENDARY"), slot="TWOHWEAPON")
    sent_params = route.calls.last.request.url.params
    assert sent_params.get_list("quality") == ["EPIC", "LEGENDARY"]
    assert sent_params["q"] == "bless"
    assert sent_params["page"] == "2"
    assert sent_params["slot"] == "TWOHWEAPON"


@respx.mock
async def test_search_items_raises_api_error_on_503(api: SideQuestAPI) -> None:
    respx.get(ITEMS_URL).mock(return_value=httpx.Response(503, json={"detail": "Battle.net unavailable"}))
    with pytest.raises(APIError) as excinfo:
        await api.search_items("bless")
    assert excinfo.value.status_code == 503


@respx.mock
async def test_get_item_parses_tooltip(api: SideQuestAPI) -> None:
    route = respx.get(f"{ITEMS_URL}/19019").mock(return_value=httpx.Response(200, json=_ITEM_JSON))
    item = await api.get_item(19019)
    assert route.call_count == 1
    assert item.name == "Thunderfury, Blessed Blade of the Windseeker"
    assert item.tooltip == ["Binds when picked up", "Unique", "One-Hand Sword"]
    assert item.sell_price == 50000


@respx.mock
async def test_get_item_raises_not_found_for_unknown_item(api: SideQuestAPI) -> None:
    respx.get(f"{ITEMS_URL}/999999").mock(return_value=httpx.Response(404, json={"detail": "not found"}))
    with pytest.raises(NotFoundError):
        await api.get_item(999999)


@respx.mock
async def test_get_item_raises_api_error_when_battlenet_unavailable(api: SideQuestAPI) -> None:
    respx.get(f"{ITEMS_URL}/19019").mock(return_value=httpx.Response(503, json={"detail": "Battle.net unavailable"}))
    with pytest.raises(APIError) as excinfo:
        await api.get_item(19019)
    assert excinfo.value.status_code == 503


ROSTER_SYNC_URL = f"{BASE}/api/guilds/1/characters/sync"
_ROSTER_SYNC_JSON = {
    "members": 8,
    "joined": ["Nox Zul"],
    "left": [],
    "profiles_refreshed": 3,
    "characters_linked": 1,
}


@respx.mock
async def test_sync_roster_posts_and_parses_report(api: SideQuestAPI) -> None:
    route = respx.post(ROSTER_SYNC_URL).mock(return_value=httpx.Response(200, json=_ROSTER_SYNC_JSON))
    report = await api.sync_roster(1)
    assert route.call_count == 1
    assert report == api_client.RosterSyncReport(
        members=8, joined=["Nox Zul"], left=[], profiles_refreshed=3, characters_linked=1
    )


@respx.mock
async def test_sync_roster_raises_not_found_for_non_site_guild(api: SideQuestAPI) -> None:
    respx.post(ROSTER_SYNC_URL).mock(return_value=httpx.Response(404, json={"detail": "not found"}))
    with pytest.raises(NotFoundError):
        await api.sync_roster(1)


@respx.mock
async def test_sync_roster_raises_api_error_when_battlenet_unavailable(api: SideQuestAPI) -> None:
    respx.post(ROSTER_SYNC_URL).mock(return_value=httpx.Response(503, json={"detail": "Battle.net unavailable"}))
    with pytest.raises(APIError) as excinfo:
        await api.sync_roster(1)
    assert excinfo.value.status_code == 503


CHARACTERS_URL = f"{BASE}/api/characters/"
_CHARACTER_SUMMARY_JSON = {
    "slug": "nox-zul",
    "name": "Nox",
    "surname": "Zul",
    "level": 60,
    "class": "mage",
    "race": "Gnome",
    "faction": "Alliance",
    "guild_name": "SideQuest",
    "guild_slug": "sidequest",
    "in_roster": True,
    "guild_rank": 0,
    "rank_name": "Guild Master",
    "item_level": 70,
    "spec": "Frost",
    "achievement_points": 500,
    "honorable_kills": 10,
    "pvp_rank": 3,
    "last_login_at": "2026-01-01T00:00:00+00:00",
    "profile_found": True,
    "fetched_at": "2026-01-01T00:00:00+00:00",
}


@respx.mock
async def test_search_characters_without_query_omits_q(api: SideQuestAPI) -> None:
    route = respx.get(CHARACTERS_URL).mock(return_value=httpx.Response(200, json=[_CHARACTER_SUMMARY_JSON]))
    characters = await api.search_characters()
    assert route.call_count == 1
    assert "q" not in route.calls.last.request.url.params
    assert characters[0].slug == "nox-zul"
    assert characters[0].wow_class == "mage"


@respx.mock
async def test_search_characters_with_query_sends_q(api: SideQuestAPI) -> None:
    route = respx.get(CHARACTERS_URL).mock(return_value=httpx.Response(200, json=[_CHARACTER_SUMMARY_JSON]))
    await api.search_characters("nox")
    assert route.calls.last.request.url.params["q"] == "nox"


@respx.mock
async def test_get_character_parses_page_with_sheet(api: SideQuestAPI) -> None:
    data = {
        **_CHARACTER_SUMMARY_JSON,
        "sheet": {
            "gear": [
                {"slot_name": "Head", "name": "Lionheart Helm", "item_level": 65},
                {"slot_name": "Chest", "name": "Breastplate of Valor", "item_level": 60},
            ],
            "hunter_pets": [
                {"name": "Boar", "level": 19, "creature": "Great Goretusk", "creature_id": 1234, "active": True}
            ],
        },
    }
    route = respx.get(f"{BASE}/api/characters/nox-zul").mock(return_value=httpx.Response(200, json=data))
    character = await api.get_character("nox-zul")
    assert route.call_count == 1
    assert character.sheet is not None
    assert len(character.sheet.gear) == 2
    assert character.sheet.gear[0].name == "Lionheart Helm"
    assert len(character.sheet.hunter_pets) == 1
    pet = character.sheet.hunter_pets[0]
    assert pet.name == "Boar"
    assert pet.creature == "Great Goretusk"
    assert pet.active is True


@respx.mock
async def test_get_character_sheet_defaults_empty_hunter_pets_when_omitted(api: SideQuestAPI) -> None:
    """A site that hasn't deployed ``hunter_pets`` yet (rolling deploy) omits it; the bot defaults to empty."""
    data = {**_CHARACTER_SUMMARY_JSON, "sheet": {"gear": []}}
    respx.get(f"{BASE}/api/characters/nox-zul").mock(return_value=httpx.Response(200, json=data))
    character = await api.get_character("nox-zul")
    assert character.sheet is not None
    assert character.sheet.hunter_pets == []


@respx.mock
async def test_get_character_parses_page_without_profile(api: SideQuestAPI) -> None:
    data = {**_CHARACTER_SUMMARY_JSON, "profile_found": False, "sheet": None}
    respx.get(f"{BASE}/api/characters/nox-zul").mock(return_value=httpx.Response(200, json=data))
    character = await api.get_character("nox-zul")
    assert character.profile_found is False
    assert character.sheet is None


@respx.mock
async def test_get_character_raises_not_found(api: SideQuestAPI) -> None:
    respx.get(f"{BASE}/api/characters/zzz-zzz").mock(
        return_value=httpx.Response(404, json={"detail": "Battle.net has no character zzz-zzz."})
    )
    with pytest.raises(NotFoundError) as excinfo:
        await api.get_character("zzz-zzz")
    assert excinfo.value.detail == "Battle.net has no character zzz-zzz."


@respx.mock
async def test_get_character_raises_api_error_when_battlenet_unavailable(api: SideQuestAPI) -> None:
    respx.get(f"{BASE}/api/characters/nox-zul").mock(
        return_value=httpx.Response(503, json={"detail": "Battle.net unavailable"})
    )
    with pytest.raises(APIError) as excinfo:
        await api.get_character("nox-zul")
    assert excinfo.value.status_code == 503


PENDING_MILESTONES_URL = f"{BASE}/api/guilds/1/characters/milestones/pending"
_MILESTONE_ID = str(uuid4())
_MILESTONE_JSON = {
    "id": _MILESTONE_ID,
    "slug": "nox-zul",
    "name": "Nox",
    "surname": "Zul",
    "class": "mage",
    "level": 60,
    "reached_at": "2026-01-01T00:00:00+00:00",
    "posted_at": None,
}


@respx.mock
async def test_list_pending_milestones_parses_list(api: SideQuestAPI) -> None:
    route = respx.get(PENDING_MILESTONES_URL).mock(return_value=httpx.Response(200, json=[_MILESTONE_JSON]))
    milestones = await api.list_pending_milestones(1)
    assert route.call_count == 1
    assert len(milestones) == 1
    assert milestones[0].slug == "nox-zul"
    assert milestones[0].level == 60


@respx.mock
async def test_list_pending_milestones_raises_not_found_for_non_site_guild(api: SideQuestAPI) -> None:
    respx.get(PENDING_MILESTONES_URL).mock(return_value=httpx.Response(404, json={"detail": "not found"}))
    with pytest.raises(NotFoundError):
        await api.list_pending_milestones(1)


@respx.mock
async def test_mark_milestone_posted_sends_message_id(api: SideQuestAPI) -> None:
    route = respx.post(f"{PENDING_MILESTONES_URL.replace('/pending', '')}/{_MILESTONE_ID}/posted").mock(
        return_value=httpx.Response(204)
    )
    await api.mark_milestone_posted(1, UUID(_MILESTONE_ID), 999)
    assert route.call_count == 1
    assert json.loads(route.calls.last.request.content) == {"discord_message_id": 999}


@respx.mock
async def test_mark_milestone_posted_raises_not_found_for_unknown_milestone(api: SideQuestAPI) -> None:
    url = f"{PENDING_MILESTONES_URL.replace('/pending', '')}/{_MILESTONE_ID}/posted"
    respx.post(url).mock(return_value=httpx.Response(404, json={"detail": "not found"}))
    with pytest.raises(NotFoundError):
        await api.mark_milestone_posted(1, UUID(_MILESTONE_ID), None)
