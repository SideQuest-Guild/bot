import json
from uuid import uuid4

import httpx
import pytest
import respx
from tenacity import wait_none

from sidequest_bot import api_client
from sidequest_bot.api_client import APIError, APIResponseShapeError, APIUnavailableError, NotFoundError, SideQuestAPI
from sidequest_common.schemas import (
    CaseAction,
    DiscordEventReconcile,
    DiscordEventState,
    DiscordEventStatus,
    DiscordLiveReport,
    DiscordPushAck,
    GuildRoleIn,
    MemberLink,
    ModCaseCreate,
)

BASE = "http://api.test"
CONFIG_URL = f"{BASE}/api/guilds/1/config"
EVENT_SYNC_URL = f"{BASE}/api/guilds/1/event-sync"
CASES_URL = f"{BASE}/api/guilds/1/cases"
MEMBER_LINKS_URL = f"{BASE}/api/guilds/1/member-links"


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
async def test_put_guild_roles_puts_full_set(api: SideQuestAPI) -> None:
    route = respx.put(f"{BASE}/api/guilds/1/roles").mock(return_value=httpx.Response(204))
    roles = [GuildRoleIn(id=1, name="Officer", color=16711680, position=2, managed=False)]
    await api.put_guild_roles(1, roles)
    assert route.call_count == 1
    sent = json.loads(route.calls.last.request.content)
    assert sent == {"roles": [{"id": 1, "name": "Officer", "color": 16711680, "position": 2, "managed": False}]}
