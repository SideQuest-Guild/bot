import httpx
import pytest
import respx
from tenacity import wait_none

from sidequest_bot import api_client
from sidequest_bot.api_client import APIError, APIUnavailableError, NotFoundError, SideQuestAPI
from sidequest_common.schemas import CaseAction, ModCaseCreate

BASE = "http://api.test"
CONFIG_URL = f"{BASE}/api/guilds/1/config"
CASES_URL = f"{BASE}/api/guilds/1/cases"


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
