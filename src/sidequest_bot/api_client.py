"""Typed HTTP client for the SideQuest API. The only way the bot talks to the API."""

import logging
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, Self

import httpx
from pydantic import TypeAdapter, ValidationError
from tenacity import AsyncRetrying, before_sleep_log, retry_if_exception, stop_after_attempt, wait_exponential

from sidequest_common.schemas import (
    ActivityAck,
    ConfirmByDiscordRequest,
    DiscordEventReconcile,
    DiscordEventState,
    DiscordInterestReport,
    DiscordLiveReport,
    DiscordPushAck,
    DiscordSyncPending,
    EventSummary,
    GuildConfigSchema,
    GuildConfigUpdate,
    GuildRoleIn,
    GuildRolesReplace,
    LeaderboardPage,
    LevelRoleSchema,
    LevelRoleUpsert,
    MemberLevelSchema,
    MemberLink,
    MemberListing,
    MemberProfileIn,
    MemberProfilesReplace,
    ModCaseCreate,
    ModCaseSchema,
    ModCaseUpdate,
    RankSchema,
    RoleMenuCreate,
    RoleMenuOptionUpsert,
    RoleMenuSchema,
    RoleMenuUpdate,
    SetXPRequest,
    SignupNoticeAck,
    SignupNoticesPage,
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
_MEMBER_LINKS = TypeAdapter(list[MemberLink])


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


class APIResponseShapeError(APIError):
    """The API answered, but not in the shape this bot expects: usually the other half of a rolling deploy.

    Raised as an APIError (status 502) so every caller's existing APIError handling covers it; a bare pydantic
    ValidationError escaping a discord.py ``tasks.loop`` would stop that loop until the bot restarts.
    """


def _parse[T](validate: Callable[[object], T], data: object) -> T:
    """Validate an API response with ``validate``, turning a schema mismatch into :class:`APIResponseShapeError`."""
    try:
        return validate(data)
    except ValidationError as exc:
        detail = f"unexpected response shape ({exc.error_count()} error(s)): {exc.errors()[0]['loc']}"
        raise APIResponseShapeError(HTTPStatus.BAD_GATEWAY, detail) from exc


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
        return _parse(GuildConfigSchema.model_validate, data)

    async def update_guild_config(self, guild_id: int, changes: GuildConfigUpdate) -> GuildConfigSchema:
        """Apply only the fields set on ``changes``."""
        data = await self._request("PATCH", f"{self._guild(guild_id)}/config", body=changes, exclude_unset=True)
        return _parse(GuildConfigSchema.model_validate, data)

    # --- Member links

    async def list_member_links(self, guild_id: int) -> list[MemberLink]:
        """Every signed-in member of ``guild_id``, and whether they've also linked Battle.net."""
        data = await self._request("GET", f"{self._guild(guild_id)}/member-links")
        return _parse(_MEMBER_LINKS.validate_python, data)

    # --- Leveling

    async def award_xp(self, guild_id: int, user_id: int) -> XPAwardResult:
        """Award one message's worth of XP (API enforces cooldown and amount)."""
        data = await self._request("POST", f"{self._guild(guild_id)}/members/{user_id}/xp")
        return _parse(XPAwardResult.model_validate, data)

    async def set_xp(self, guild_id: int, user_id: int, xp: int) -> MemberLevelSchema:
        """Overwrite a member's XP total; level is recomputed."""
        data = await self._request("PUT", f"{self._guild(guild_id)}/members/{user_id}/xp", body=SetXPRequest(xp=xp))
        return _parse(MemberLevelSchema.model_validate, data)

    async def get_rank(self, guild_id: int, user_id: int) -> RankSchema:
        """A member's rank; members without XP get ``rank=None`` and zeros."""
        data = await self._request("GET", f"{self._guild(guild_id)}/members/{user_id}/rank")
        return _parse(RankSchema.model_validate, data)

    async def get_leaderboard(self, guild_id: int, *, limit: int = 10, offset: int = 0) -> LeaderboardPage:
        """Highest XP first."""
        data = await self._request(
            "GET", f"{self._guild(guild_id)}/leaderboard", params={"limit": limit, "offset": offset}
        )
        return _parse(LeaderboardPage.model_validate, data)

    async def list_level_roles(self, guild_id: int) -> list[LevelRoleSchema]:
        """Level rewards, lowest level first."""
        return _parse(_LEVEL_ROLES.validate_python, await self._request("GET", f"{self._guild(guild_id)}/level-roles"))

    async def set_level_role(self, guild_id: int, level: int, role_id: int) -> LevelRoleSchema:
        """Grant ``role_id`` at ``level`` (replaces any existing reward for that level)."""
        data = await self._request(
            "PUT", f"{self._guild(guild_id)}/level-roles/{level}", body=LevelRoleUpsert(role_id=role_id)
        )
        return _parse(LevelRoleSchema.model_validate, data)

    async def delete_level_role(self, guild_id: int, level: int) -> None:
        """Remove the reward at ``level``. Raises NotFoundError if none."""
        await self._request("DELETE", f"{self._guild(guild_id)}/level-roles/{level}")

    # --- Moderation cases

    async def create_case(self, guild_id: int, case: ModCaseCreate) -> ModCaseSchema:
        """Record a moderation action; the API assigns the case number."""
        return _parse(
            ModCaseSchema.model_validate, await self._request("POST", f"{self._guild(guild_id)}/cases", body=case)
        )

    async def list_cases(
        self, guild_id: int, *, user_id: int | None = None, limit: int = 25, offset: int = 0
    ) -> list[ModCaseSchema]:
        """Newest first, optionally only for ``user_id``."""
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if user_id is not None:
            params["user_id"] = user_id
        return _parse(
            _CASES.validate_python, await self._request("GET", f"{self._guild(guild_id)}/cases", params=params)
        )

    async def get_case(self, guild_id: int, case_number: int) -> ModCaseSchema:
        """One case. Raises NotFoundError if missing."""
        data = await self._request("GET", f"{self._guild(guild_id)}/cases/{case_number}")
        return _parse(ModCaseSchema.model_validate, data)

    async def update_case(self, guild_id: int, case_number: int, changes: ModCaseUpdate) -> ModCaseSchema:
        """Edit a case (currently just its reason)."""
        data = await self._request(
            "PATCH", f"{self._guild(guild_id)}/cases/{case_number}", body=changes, exclude_unset=True
        )
        return _parse(ModCaseSchema.model_validate, data)

    # --- Role menus

    async def create_role_menu(self, guild_id: int, menu: RoleMenuCreate) -> RoleMenuSchema:
        """Create a menu (no message yet; PATCH ``message_id`` after posting)."""
        data = await self._request("POST", f"{self._guild(guild_id)}/role-menus", body=menu)
        return _parse(RoleMenuSchema.model_validate, data)

    async def list_role_menus(self, guild_id: int) -> list[RoleMenuSchema]:
        """All menus in the guild."""
        return _parse(_ROLE_MENUS.validate_python, await self._request("GET", f"{self._guild(guild_id)}/role-menus"))

    async def get_role_menu(self, guild_id: int, menu_id: UUID) -> RoleMenuSchema:
        """One menu with its options."""
        data = await self._request("GET", f"{self._guild(guild_id)}/role-menus/{menu_id}")
        return _parse(RoleMenuSchema.model_validate, data)

    async def update_role_menu(self, guild_id: int, menu_id: UUID, changes: RoleMenuUpdate) -> RoleMenuSchema:
        """Apply only the fields set on ``changes``."""
        data = await self._request(
            "PATCH", f"{self._guild(guild_id)}/role-menus/{menu_id}", body=changes, exclude_unset=True
        )
        return _parse(RoleMenuSchema.model_validate, data)

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
        return _parse(RoleMenuSchema.model_validate, data)

    async def delete_role_menu_option(self, guild_id: int, menu_id: UUID, role_id: int) -> RoleMenuSchema:
        """Remove the button for ``role_id``; returns the updated menu."""
        data = await self._request("DELETE", f"{self._guild(guild_id)}/role-menus/{menu_id}/options/{role_id}")
        return _parse(RoleMenuSchema.model_validate, data)

    # --- Event sync

    async def get_pending_event_sync(self, guild_id: int) -> DiscordSyncPending:
        """Pushes and deletions the bot should apply to Discord now."""
        data = await self._request("GET", f"{self._guild(guild_id)}/event-sync")
        return _parse(DiscordSyncPending.model_validate, data)

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

    async def report_event_interest(self, guild_id: int, discord_event_id: int, report: DiscordInterestReport) -> None:
        """Replace the interested-user set for the guild event linked to ``discord_event_id``; no-op if unlinked."""
        await self._request("PUT", f"{self._guild(guild_id)}/event-sync/interest/{discord_event_id}", body=report)

    async def ack_event_activity(self, guild_id: int, ids: list[UUID]) -> None:
        """Confirm these sign-up activity rows were posted in their event's forum thread."""
        await self._request("POST", f"{self._guild(guild_id)}/event-sync/activity/ack", body=ActivityAck(ids=ids))

    async def list_upcoming_events(self) -> list[EventSummary]:
        """Upcoming guild events, soonest first (the public ``/api/events`` list; also materializes repeats)."""
        data = await self._request("GET", "/api/events", params={"when": "upcoming"})
        return _parse(_EVENTS.validate_python, data)

    # --- Live status

    async def report_discord_live(self, guild_id: int, report: DiscordLiveReport) -> None:
        """Push the current voice/presence snapshot for ``guild_id``."""
        await self._request("PUT", f"{self._guild(guild_id)}/live", body=report)

    async def put_guild_roles(self, guild_id: int, roles: list[GuildRoleIn]) -> None:
        """Replace ``guild_id``'s full role set (excluding ``@everyone``, dropped by the caller)."""
        await self._request("PUT", f"{self._guild(guild_id)}/roles", body=GuildRolesReplace(roles=roles))

    async def put_member_profiles(
        self, guild_id: int, profiles: list[MemberProfileIn], departed_ids: list[int]
    ) -> None:
        """Push linked members' current Discord profiles and report linked members who've left."""
        await self._request(
            "PUT",
            f"{self._guild(guild_id)}/member-profiles",
            body=MemberProfilesReplace(profiles=profiles, departed_ids=departed_ids),
        )

    # --- Signup notices

    async def list_signup_notices(self, guild_id: int) -> SignupNoticesPage:
        """Members needing a new notice posted, and posted notices whose message is now stale."""
        data = await self._request("GET", f"{self._guild(guild_id)}/signup-notices")
        return _parse(SignupNoticesPage.model_validate, data)

    async def ack_signup_notice(self, guild_id: int, member_id: int, ack: SignupNoticeAck) -> None:
        """Confirm the bot's message for ``member_id`` now reflects their current roster state."""
        await self._request("POST", f"{self._guild(guild_id)}/signup-notices/{member_id}/ack", body=ack)

    async def confirm_roster_by_discord(
        self, guild_id: int, discord_id: int, request: ConfirmByDiscordRequest
    ) -> MemberListing:
        """Confirm a member on the roster, with the acting GM identified by Discord id."""
        data = await self._request(
            "PUT", f"{self._guild(guild_id)}/members/by-discord/{discord_id}/roster-confirmation", body=request
        )
        return _parse(MemberListing.model_validate, data)

    async def unconfirm_roster_by_discord(
        self, guild_id: int, discord_id: int, request: ConfirmByDiscordRequest
    ) -> MemberListing:
        """Clear a member's roster confirmation, with the acting GM identified by Discord id."""
        data = await self._request(
            "DELETE", f"{self._guild(guild_id)}/members/by-discord/{discord_id}/roster-confirmation", body=request
        )
        return _parse(MemberListing.model_validate, data)
