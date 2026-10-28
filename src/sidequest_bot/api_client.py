"""Typed HTTP client for the SideQuest API. The only way the bot talks to the API."""

import logging
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, Self

import httpx
from pydantic import TypeAdapter
from tenacity import AsyncRetrying, before_sleep_log, retry_if_exception, stop_after_attempt, wait_exponential

from sidequest_common.schemas import (
    DiscordEventReconcile,
    DiscordEventState,
    DiscordPushAck,
    DiscordSyncPending,
    EventSummary,
    GuildConfigSchema,
    GuildConfigUpdate,
    LeaderboardPage,
    LevelRoleSchema,
    LevelRoleUpsert,
    MemberLevelSchema,
    ModCaseCreate,
    ModCaseSchema,
    ModCaseUpdate,
    RankSchema,
    RoleMenuCreate,
    RoleMenuOptionUpsert,
    RoleMenuSchema,
    RoleMenuUpdate,
    SetXPRequest,
    XPAwardResult,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import TracebackType
    from uuid import UUID

    from pydantic import BaseModel

logger = logging.getLogger(__name__)

API_KEY_HEADER = "X-API-Key"

_LEVEL_ROLES = TypeAdapter(list[LevelRoleSchema])
_CASES = TypeAdapter(list[ModCaseSchema])
_ROLE_MENUS = TypeAdapter(list[RoleMenuSchema])
_EVENTS = TypeAdapter(list[EventSummary])


class APIError(Exception):
    """The API returned an error response, or couldn't be reached. Every client failure is an APIError."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"API error {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


class NotFoundError(APIError):
    """The API returned 404."""


class APIUnavailableError(APIError):
    """The API couldn't be reached (connection/timeout) after retries. Reported as status 503."""


def _is_retryable(exc: BaseException) -> bool:
    """Idempotent requests: retry any transport error or 5xx."""
    if isinstance(exc, httpx.TransportError):
        return True
    return isinstance(exc, APIError) and exc.status_code >= HTTPStatus.INTERNAL_SERVER_ERROR


def _never_sent(exc: BaseException) -> bool:
    """Non-idempotent requests (POST): retry only if the request never reached the server."""
    return isinstance(exc, httpx.ConnectError | httpx.ConnectTimeout)


def _retrying(predicate: Callable[[BaseException], bool]) -> AsyncRetrying:
    return AsyncRetrying(
        retry=retry_if_exception(predicate),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, max=4),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )


class SideQuestAPI:
    """Async client for the SideQuest API.

    Retries 3x with backoff: idempotent methods on transport errors/5xx, POST only when the connection never opened
    (so a case or XP award is never recorded twice). 4xx is never retried.

    Pass ``client`` to reuse an existing ``httpx.AsyncClient`` (tests pass Litestar's ``AsyncTestClient``).
    """

    def __init__(self, base_url: str, api_key: str, *, client: httpx.AsyncClient | None = None) -> None:
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(base_url=base_url, timeout=10.0)
        self._headers = {API_KEY_HEADER: api_key}

    async def aclose(self) -> None:
        """Close the underlying HTTP client if this instance created it."""
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        body: BaseModel | None = None,
        params: dict[str, Any] | None = None,
        exclude_unset: bool = False,
    ) -> Any:
        json = body.model_dump(mode="json", exclude_unset=exclude_unset) if body is not None else None
        retrying = _retrying(_never_sent if method == "POST" else _is_retryable)
        try:
            return await retrying(self._send, method, path, json=json, params=params)
        except httpx.TransportError as exc:
            raise APIUnavailableError(HTTPStatus.SERVICE_UNAVAILABLE, f"API unreachable: {exc!r}") from exc

    async def _send(self, method: str, path: str, *, json: Any, params: dict[str, Any] | None) -> Any:
        response = await self._client.request(method, path, json=json, params=params, headers=self._headers)
        if response.status_code == HTTPStatus.NO_CONTENT:
            return None
        if response.is_error:
            try:
                detail = str(response.json().get("detail", response.text))
            except ValueError:
                detail = response.text
            error_cls = NotFoundError if response.status_code == HTTPStatus.NOT_FOUND else APIError
            raise error_cls(response.status_code, detail)
        return response.json()

    @staticmethod
    def _guild(guild_id: int) -> str:
        return f"/api/guilds/{guild_id}"

    # --- Guild config

    async def get_guild_config(self, guild_id: int) -> GuildConfigSchema:
        """Config for ``guild_id`` (created with defaults on first access)."""
        data = await self._request("GET", f"{self._guild(guild_id)}/config")
        return GuildConfigSchema.model_validate(data)

    async def update_guild_config(self, guild_id: int, changes: GuildConfigUpdate) -> GuildConfigSchema:
        """Apply only the fields set on ``changes``."""
        data = await self._request("PATCH", f"{self._guild(guild_id)}/config", body=changes, exclude_unset=True)
        return GuildConfigSchema.model_validate(data)

    # --- Leveling

    async def award_xp(self, guild_id: int, user_id: int) -> XPAwardResult:
        """Award one message's worth of XP (API enforces cooldown and amount)."""
        data = await self._request("POST", f"{self._guild(guild_id)}/members/{user_id}/xp")
        return XPAwardResult.model_validate(data)

    async def set_xp(self, guild_id: int, user_id: int, xp: int) -> MemberLevelSchema:
        """Overwrite a member's XP total; level is recomputed."""
        data = await self._request("PUT", f"{self._guild(guild_id)}/members/{user_id}/xp", body=SetXPRequest(xp=xp))
        return MemberLevelSchema.model_validate(data)

    async def get_rank(self, guild_id: int, user_id: int) -> RankSchema:
        """A member's rank; members without XP get ``rank=None`` and zeros."""
        data = await self._request("GET", f"{self._guild(guild_id)}/members/{user_id}/rank")
        return RankSchema.model_validate(data)

    async def get_leaderboard(self, guild_id: int, *, limit: int = 10, offset: int = 0) -> LeaderboardPage:
        """Highest XP first."""
        data = await self._request(
            "GET", f"{self._guild(guild_id)}/leaderboard", params={"limit": limit, "offset": offset}
        )
        return LeaderboardPage.model_validate(data)

    async def list_level_roles(self, guild_id: int) -> list[LevelRoleSchema]:
        """Level rewards, lowest level first."""
        return _LEVEL_ROLES.validate_python(await self._request("GET", f"{self._guild(guild_id)}/level-roles"))

    async def set_level_role(self, guild_id: int, level: int, role_id: int) -> LevelRoleSchema:
        """Grant ``role_id`` at ``level`` (replaces any existing reward for that level)."""
        data = await self._request(
            "PUT", f"{self._guild(guild_id)}/level-roles/{level}", body=LevelRoleUpsert(role_id=role_id)
        )
        return LevelRoleSchema.model_validate(data)

    async def delete_level_role(self, guild_id: int, level: int) -> None:
        """Remove the reward at ``level``. Raises NotFoundError if none."""
        await self._request("DELETE", f"{self._guild(guild_id)}/level-roles/{level}")

    # --- Moderation cases

    async def create_case(self, guild_id: int, case: ModCaseCreate) -> ModCaseSchema:
        """Record a moderation action; the API assigns the case number."""
        return ModCaseSchema.model_validate(await self._request("POST", f"{self._guild(guild_id)}/cases", body=case))

    async def list_cases(
        self, guild_id: int, *, user_id: int | None = None, limit: int = 25, offset: int = 0
    ) -> list[ModCaseSchema]:
        """Newest first, optionally only for ``user_id``."""
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if user_id is not None:
            params["user_id"] = user_id
        return _CASES.validate_python(await self._request("GET", f"{self._guild(guild_id)}/cases", params=params))

    async def get_case(self, guild_id: int, case_number: int) -> ModCaseSchema:
        """One case. Raises NotFoundError if missing."""
        data = await self._request("GET", f"{self._guild(guild_id)}/cases/{case_number}")
        return ModCaseSchema.model_validate(data)

    async def update_case(self, guild_id: int, case_number: int, changes: ModCaseUpdate) -> ModCaseSchema:
        """Edit a case (currently just its reason)."""
        data = await self._request(
            "PATCH", f"{self._guild(guild_id)}/cases/{case_number}", body=changes, exclude_unset=True
        )
        return ModCaseSchema.model_validate(data)

    # --- Role menus

    async def create_role_menu(self, guild_id: int, menu: RoleMenuCreate) -> RoleMenuSchema:
        """Create a menu (no message yet; PATCH ``message_id`` after posting)."""
        data = await self._request("POST", f"{self._guild(guild_id)}/role-menus", body=menu)
        return RoleMenuSchema.model_validate(data)

    async def list_role_menus(self, guild_id: int) -> list[RoleMenuSchema]:
        """All menus in the guild."""
        return _ROLE_MENUS.validate_python(await self._request("GET", f"{self._guild(guild_id)}/role-menus"))

    async def get_role_menu(self, guild_id: int, menu_id: UUID) -> RoleMenuSchema:
        """One menu with its options."""
        data = await self._request("GET", f"{self._guild(guild_id)}/role-menus/{menu_id}")
        return RoleMenuSchema.model_validate(data)

    async def update_role_menu(self, guild_id: int, menu_id: UUID, changes: RoleMenuUpdate) -> RoleMenuSchema:
        """Apply only the fields set on ``changes``."""
        data = await self._request(
            "PATCH", f"{self._guild(guild_id)}/role-menus/{menu_id}", body=changes, exclude_unset=True
        )
        return RoleMenuSchema.model_validate(data)

    async def delete_role_menu(self, guild_id: int, menu_id: UUID) -> None:
        """Delete a menu and its options."""
        await self._request("DELETE", f"{self._guild(guild_id)}/role-menus/{menu_id}")

    async def upsert_role_menu_option(
        self, guild_id: int, menu_id: UUID, role_id: int, option: RoleMenuOptionUpsert
    ) -> RoleMenuSchema:
        """Add or edit the button for ``role_id``; returns the updated menu."""
        data = await self._request(
            "PUT", f"{self._guild(guild_id)}/role-menus/{menu_id}/options/{role_id}", body=option
        )
        return RoleMenuSchema.model_validate(data)

    async def delete_role_menu_option(self, guild_id: int, menu_id: UUID, role_id: int) -> RoleMenuSchema:
        """Remove the button for ``role_id``; returns the updated menu."""
        data = await self._request("DELETE", f"{self._guild(guild_id)}/role-menus/{menu_id}/options/{role_id}")
        return RoleMenuSchema.model_validate(data)

    # --- Event sync

    async def get_pending_event_sync(self, guild_id: int) -> DiscordSyncPending:
        """Pushes and deletions the bot should apply to Discord now."""
        data = await self._request("GET", f"{self._guild(guild_id)}/event-sync")
        return DiscordSyncPending.model_validate(data)

    async def ack_event_push(self, guild_id: int, event_id: UUID, ack: DiscordPushAck) -> None:
        """Confirm ``ack.push_hash`` was applied (or skipped) for ``event_id``."""
        await self._request("POST", f"{self._guild(guild_id)}/event-sync/{event_id}/ack", body=ack)

    async def report_discord_event(self, guild_id: int, state: DiscordEventState) -> None:
        """Report that Discord now holds ``state`` for one event (created, edited, or read off the gateway)."""
        await self._request("POST", f"{self._guild(guild_id)}/event-sync/events", body=state)

    async def report_discord_event_deleted(self, guild_id: int, event_id: int) -> None:
        """Report that ``event_id`` was deleted in Discord."""
        await self._request("DELETE", f"{self._guild(guild_id)}/event-sync/events/{event_id}")

    async def delete_event_sync_tombstone(self, guild_id: int, event_id: int) -> None:
        """Clear the tombstone for ``event_id`` after deleting it in Discord."""
        await self._request("DELETE", f"{self._guild(guild_id)}/event-sync/tombstones/{event_id}")

    async def reconcile_event_sync(self, guild_id: int, reconcile: DiscordEventReconcile) -> None:
        """Report every scheduled event currently in the guild."""
        await self._request("POST", f"{self._guild(guild_id)}/event-sync/reconcile", body=reconcile)

    async def list_upcoming_events(self) -> list[EventSummary]:
        """Upcoming guild events, soonest first (the public ``/api/events`` list; also materializes repeats)."""
        data = await self._request("GET", "/api/events", params={"when": "upcoming"})
        return _EVENTS.validate_python(data)
