"""Typed HTTP client for the SideQuest API. The only way the bot talks to the API."""

import logging
from datetime import date, datetime
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, Self
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError
from tenacity import AsyncRetrying, before_sleep_log, retry_if_exception, stop_after_attempt, wait_exponential

from sidequest_common.schemas import (
    AchievementCategorySchema,
    AchievementCategoryTree,
    AchievementSchema,
    ActivityAck,
    AddonSchema,
    AnnouncementPosted,
    AwayNowReport,
    BadgeAck,
    BadgeSyncReport,
    ConfirmByDiscordRequest,
    CouncilLogEntry,
    CouncilLogKind,
    CouncilLogPage,
    CouncilPostedBody,
    DiscordEventReconcile,
    DiscordEventState,
    DiscordInterestReport,
    DiscordLiveReport,
    DiscordPushAck,
    DiscordRosterConfirmRequest,
    DiscordSyncPending,
    EventSummary,
    GroupPresenceReport,
    GroupSessionClose,
    GroupSessionCreate,
    GroupSessionSchema,
    GroupSessionUpdate,
    GroupTranscriptPurge,
    GuildConfigSchema,
    GuildConfigUpdate,
    GuildCrafters,
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
    MessageBackfill,
    MessageBackfillResult,
    ModCaseCreate,
    ModCaseSchema,
    ModCaseUpdate,
    PendingAnnouncement,
    PendingAnnouncementsPage,
    RankSchema,
    RecipeDetail,
    Recruiting,
    RecruitShoutoutAck,
    RoleMenuCreate,
    RoleMenuOptionUpsert,
    RoleMenuSchema,
    RoleMenuUpdate,
    RoleResetCandidate,
    SetXPRequest,
    Signup,
    SignupByDiscord,
    SignupNoticeAck,
    SignupNoticesPage,
    SignupUpsert,
    VoicePresenceReport,
    VoiceXPRequest,
    WeeklyBoardEvent,
    WeeklyBoardMessage,
    XPActivityReport,
    XPAwardResult,
    XPGrantResult,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from types import TracebackType

logger = logging.getLogger(__name__)

API_KEY_HEADER = "X-API-Key"

_LEVEL_ROLES = TypeAdapter(list[LevelRoleSchema])
_CASES = TypeAdapter(list[ModCaseSchema])
_ROLE_MENUS = TypeAdapter(list[RoleMenuSchema])
_EVENTS = TypeAdapter(list[EventSummary])
_MEMBER_LINKS = TypeAdapter(list[MemberLink])
_ROLE_RESETS = TypeAdapter(list[RoleResetCandidate])
_WEEKLY_BOARD_EVENTS = TypeAdapter(list[WeeklyBoardEvent])
_GROUP_SESSIONS = TypeAdapter(list[GroupSessionSchema])
_XP_GRANTS = TypeAdapter(list[XPGrantResult])
_ADDONS = TypeAdapter(list[AddonSchema])
_ACHIEVEMENT_CATEGORIES = TypeAdapter(list[AchievementCategoryTree])


class ItemSummary(BaseModel):
    """One item in a search result (``GET /api/items``); no icon or tooltip.

    Mirrors the website's ``ItemSearchResult`` by hand: the item schemas aren't in the ``sidequest-common`` pin yet.
    """

    item_id: int
    name: str
    quality: str = Field(description="Battle.net's quality constant, such as `EPIC`.")
    quality_name: str = Field(description="The quality as the game shows it, such as `Epic`.")
    inventory_type: str = Field(description="Battle.net's slot constant, such as `TWOHWEAPON`.")
    inventory_type_name: str = Field(description="The slot as the game shows it, such as `Two-Hand`.")
    item_level: int
    required_level: int


class ItemSearchPage(BaseModel):
    """One page of item search results (``GET /api/items``). Mirrors the website's ``ItemSearchPage`` by hand."""

    page: int
    page_count: int
    results: list[ItemSummary]


class Item(BaseModel):
    """An item with its tooltip (``GET /api/items/{item_id}``). Mirrors the website's ``ItemSchema`` by hand."""

    item_id: int
    name: str
    quality: str = Field(description="Battle.net's quality constant, such as `EPIC`.")
    quality_name: str = Field(description="The quality as the game shows it, such as `Epic`.")
    item_class: str
    item_subclass: str
    inventory_type: str = Field(description="Battle.net's slot constant, such as `TWOHWEAPON`.")
    inventory_type_name: str = Field(description="The slot as the game shows it, such as `Two-Hand`.")
    item_level: int
    required_level: int
    sell_price: int = Field(description="What a vendor pays, in copper.")
    is_equippable: bool
    icon_url: str | None
    tooltip: list[str] = Field(description="The tooltip's text lines in order, below the item's name.")


class RosterSyncReport(BaseModel):
    """``POST /api/guilds/{id}/characters/sync``.

    Mirrors the website's ``RosterSyncReport`` by hand: phase 3's character schemas aren't in the
    ``sidequest-common`` pin yet.
    """

    members: int
    joined: list[str]
    left: list[str]
    profiles_refreshed: int
    characters_linked: int
    members_confirmed: int = 0
    """Members newly confirmed on the guild roster because one of their Battle.net characters is in it."""


class CharacterGearItem(BaseModel):
    """One equipped item, as shown in ``/who``'s gear field.

    Mirrors a subset of the website's ``CharacterGear`` by hand: phase 4's character schemas aren't in the
    ``sidequest-common`` pin yet.
    """

    slot_name: str
    name: str
    item_level: int | None


class CharacterHunterPet(BaseModel):
    """One of a hunter's tamed pets, as shown in ``/who``'s Pets field.

    Mirrors the website's ``CharacterHunterPet`` by hand: phase 4's character schemas aren't in the
    ``sidequest-common`` pin yet. ``creature`` is the tamed beast's species name (e.g. "Great Goretusk").
    """

    name: str
    level: int
    creature: str
    creature_id: int
    active: bool = False


class CharacterSheet(BaseModel):
    """The subset of the website's ``CharacterSheet`` this bot shows: the gear list and any hunter pets.

    ``hunter_pets`` defaults empty so the bot works against a site that hasn't deployed it yet.
    """

    gear: list[CharacterGearItem]
    hunter_pets: list[CharacterHunterPet] = Field(default_factory=list)


class CharacterSummary(BaseModel):
    """A character's Battle.net summary (``GET /api/characters/`` or ``GET /api/characters/?q=``).

    Mirrors the website's ``CharacterSummary`` by hand: phase 4's character schemas aren't in the
    ``sidequest-common`` pin yet. ``profile_found`` is false while Battle.net has no profile for the character; the
    summary then holds only what the guild roster says.
    """

    model_config = ConfigDict(populate_by_name=True)

    slug: str
    name: str
    surname: str
    level: int
    wow_class: str | None = Field(default=None, alias="class")
    race: str | None = None
    faction: str | None = None
    guild_name: str | None = None
    guild_slug: str | None = None
    in_roster: bool
    guild_rank: int | None = None
    rank_name: str | None = None
    item_level: int | None = None
    spec: str | None = None
    achievement_points: int | None = None
    honorable_kills: int | None = None
    pvp_rank: int | None = None
    last_login_at: datetime | None = None
    profile_found: bool
    fetched_at: datetime | None = None


class CharacterPage(CharacterSummary):
    """A character's summary plus its sheet (``GET /api/characters/{slug}``).

    ``sheet`` is ``None`` while Battle.net has no profile for the character.
    """

    sheet: CharacterSheet | None = None


class CharacterMilestone(BaseModel):
    """A roster character reaching level 40 or 60 (``GET .../characters/milestones/pending``).

    Mirrors the website's ``CharacterMilestoneSchema`` by hand: phase 4's milestone schema isn't in the
    ``sidequest-common`` pin yet.
    """

    model_config = ConfigDict(populate_by_name=True)

    id: UUID
    slug: str
    name: str
    surname: str
    wow_class: str | None = Field(default=None, alias="class")
    level: int
    reached_at: datetime
    posted_at: datetime | None = None


class MilestonePosted(BaseModel):
    """Body for ``POST .../characters/milestones/{id}/posted``: the Discord message the bot posted."""

    discord_message_id: int | None = None


_CHARACTER_SUMMARIES = TypeAdapter(list[CharacterSummary])
_PENDING_MILESTONES = TypeAdapter(list[CharacterMilestone])

AUCTION_TREND_DAYS = 14
"""Days of daily price history ``get_item_price`` returns in ``trend`` (``sidequest-common`` ``AUCTION_TREND_DAYS``)."""
MAX_AUCTION_WATCHES = 25
"""Per-member cap on auction watches (``sidequest-common`` ``MAX_AUCTION_WATCHES``); upsert returns 409 past it."""


class AuctionHousePrice(BaseModel):
    """One auction house's current price for an item.

    Mirrors the website's ``AuctionHousePrice`` by hand: the auctions schemas aren't in the ``sidequest-common``
    pin yet.
    """

    house_id: int
    house_name: str
    unit_price: int
    quantity: int
    listings: int
    updated_at: datetime


class AuctionTrendPoint(BaseModel):
    """One day's lowest price in a 14-day trend. Mirrors the website's ``AuctionTrendPoint`` by hand."""

    day: date
    unit_price: int


class ItemAuctionPrice(BaseModel):
    """``GET /api/auctions/prices/{item_id}``: an item's current price across every faction house, plus trend.

    Mirrors the website's ``ItemAuctionPrice`` by hand. ``unit_price`` is the lowest across houses, ``None`` when
    nobody lists the item.
    """

    item_id: int
    unit_price: int | None
    quantity_total: int
    houses: list[AuctionHousePrice]
    trend: list[AuctionTrendPoint]
    updated_at: datetime | None


class AuctionSyncHouse(BaseModel):
    """One house's counts from an auction sync. Mirrors the website's ``AuctionSyncHouse`` by hand."""

    house_id: int
    house_name: str
    auctions: int
    commodities: int


class AuctionSyncReport(BaseModel):
    """``POST /api/guilds/{id}/auctions/sync``. Mirrors the website's ``AuctionSyncReport`` by hand."""

    houses: list[AuctionSyncHouse]
    items_priced: int
    alerts_queued: int


class AuctionWatch(BaseModel):
    """A member's price watch on one item. Mirrors the website's ``AuctionWatch`` by hand."""

    id: UUID
    item_id: int
    item_name: str | None
    icon_url: str | None
    max_unit_price: int
    current_unit_price: int | None
    created_at: datetime
    last_alerted_at: datetime | None


class AuctionWatchCreate(BaseModel):
    """Body for ``POST .../auction-watches``: upserts by ``item_id``. Mirrors ``AuctionWatchCreate`` by hand."""

    item_id: int = Field(gt=0)
    max_unit_price: int = Field(ge=1, le=10_000_000_000)


class AuctionAlertSchema(BaseModel):
    """A queued price alert, ready to DM. Mirrors the website's ``AuctionAlertSchema`` by hand."""

    id: UUID
    discord_user_id: int
    member_name: str
    item_id: int
    item_name: str | None
    icon_url: str | None
    unit_price: int
    max_unit_price: int
    quantity: int
    house_name: str
    created_at: datetime


_AUCTION_WATCHES = TypeAdapter(list[AuctionWatch])
_AUCTION_ALERTS = TypeAdapter(list[AuctionAlertSchema])


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

    # --- Loot council

    async def pending_council_log(self, guild_id: int) -> list[CouncilLogEntry]:
        """Council decisions and voids not yet posted, oldest first (max 25, raises ``NotFoundError`` otherwise)."""
        data = await self._request("GET", f"{self._guild(guild_id)}/loot/council/pending")
        return _parse(CouncilLogPage.model_validate, data).items

    async def ack_council_log(self, guild_id: int, loot_id: UUID, kind: CouncilLogKind) -> None:
        """Confirm ``loot_id``'s ``kind`` posted; ``pending_council_log`` stops reporting it."""
        body = CouncilPostedBody(kind=kind)
        await self._request("POST", f"{self._guild(guild_id)}/loot/council/{loot_id}/posted", body=body)

    # --- Member links

    async def list_member_links(self, guild_id: int) -> list[MemberLink]:
        """Every signed-in member of ``guild_id``, and whether they've also linked Battle.net."""
        data = await self._request("GET", f"{self._guild(guild_id)}/member-links")
        return _parse(_MEMBER_LINKS.validate_python, data)

    async def confirm_discord_member(
        self, guild_id: int, discord_id: int, request: DiscordRosterConfirmRequest
    ) -> MemberLink:
        """**Confirm in guild**: confirm a Discord-only member on the roster.

        Creates their ``Member`` row from ``request.profile`` if they have none, else refreshes it.
        """
        data = await self._request(
            "PUT", f"{self._guild(guild_id)}/discord-members/{discord_id}/roster-confirmation", body=request
        )
        return _parse(MemberLink.model_validate, data)

    # --- Away

    async def get_away_now(self, guild_id: int) -> list[int]:
        """Discord ids of members marked away today (America/Chicago)."""
        data = await self._request("GET", f"{self._guild(guild_id)}/away-now")
        return _parse(AwayNowReport.model_validate, data).discord_ids

    # --- Leveling

    async def award_xp(self, guild_id: int, user_id: int) -> XPAwardResult:
        """Award one message's worth of XP (API enforces cooldown and amount)."""
        data = await self._request("POST", f"{self._guild(guild_id)}/members/{user_id}/xp")
        return _parse(XPAwardResult.model_validate, data)

    async def set_xp(self, guild_id: int, user_id: int, xp: int) -> MemberLevelSchema:
        """Overwrite a member's XP total; level is recomputed."""
        data = await self._request("PUT", f"{self._guild(guild_id)}/members/{user_id}/xp", body=SetXPRequest(xp=xp))
        return _parse(MemberLevelSchema.model_validate, data)

    async def grant_voice_xp(self, guild_id: int, request: VoiceXPRequest) -> list[XPGrantResult]:
        """Voice XP for members the bot found eligible this minute."""
        data = await self._request("POST", f"{self._guild(guild_id)}/xp/voice", body=request)
        return _parse(_XP_GRANTS.validate_python, data)

    async def sync_activity_xp(self, guild_id: int) -> XPActivityReport:
        """Pay any newly earned event and #lfg group XP (idempotent; safe to poll)."""
        data = await self._request("POST", f"{self._guild(guild_id)}/xp/activity")
        return _parse(XPActivityReport.model_validate, data)

    async def backfill_messages(self, guild_id: int, request: MessageBackfill) -> MessageBackfillResult:
        """Push message counts read once from channel history (idempotent; keeps ``max(existing, reported)``)."""
        data = await self._request("PUT", f"{self._guild(guild_id)}/activity/messages", body=request)
        return _parse(MessageBackfillResult.model_validate, data)

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

    async def ack_recruit_shoutouts(self, guild_id: int, ids: list[UUID]) -> None:
        """Confirm these recruit shout-out rows were posted (or undeliverable) in their drive's forum thread."""
        await self._request(
            "POST", f"{self._guild(guild_id)}/event-sync/recruit-shoutouts/ack", body=RecruitShoutoutAck(ids=ids)
        )

    async def get_event_signup_by_discord(self, guild_id: int, event_id: UUID, discord_id: int) -> SignupByDiscord:
        """A member's current signup for ``event_id`` plus their most recent character/class/role, by Discord id.

        Raises ``NotFoundError`` if ``discord_id`` has never signed in on the site, or ``event_id`` doesn't exist.
        """
        data = await self._request("GET", f"{self._guild(guild_id)}/events/{event_id}/signups/by-discord/{discord_id}")
        return _parse(SignupByDiscord.model_validate, data)

    async def upsert_event_signup_by_discord(
        self, guild_id: int, event_id: UUID, discord_id: int, data: SignupUpsert
    ) -> Signup:
        """Create or replace a member's signup for ``event_id``, by Discord id (Discord's sign-up buttons).

        Raises ``NotFoundError`` if ``discord_id`` has never signed in. The caller is responsible for handling a
        409 (event not scheduled) or 400 (role kind missing character/class/role) ``APIError``.
        """
        response = await self._request(
            "PUT", f"{self._guild(guild_id)}/events/{event_id}/signups/by-discord/{discord_id}", body=data
        )
        return _parse(Signup.model_validate, response)

    async def list_upcoming_events(self) -> list[EventSummary]:
        """Upcoming guild events, soonest first (the public ``/api/events`` list; also materializes repeats)."""
        data = await self._request("GET", "/api/events", params={"when": "upcoming"})
        return _parse(_EVENTS.validate_python, data)

    async def get_event(self, event_id: UUID) -> EventSummary:
        """One event's headline fields (kind, title, ...), via the public ``/api/events/{id}`` GET.

        Raises ``NotFoundError`` if ``event_id`` is unknown. The response is actually ``EventDetail`` (a superset
        of ``EventSummary``, e.g. with the roster); validating as ``EventSummary`` keeps callers that only need the
        headline fields decoupled from that extra shape.
        """
        data = await self._request("GET", f"/api/events/{event_id}")
        return _parse(EventSummary.model_validate, data)

    async def get_recruiting(self) -> Recruiting:
        """What roles and classes the guild is recruiting for (the public ``/api/recruiting`` GET)."""
        data = await self._request("GET", "/api/recruiting")
        return _parse(Recruiting.model_validate, data)

    async def list_addons(self) -> list[AddonSchema]:
        """Every recommended addon, by name (the public ``/api/addons`` GET)."""
        data = await self._request("GET", "/api/addons")
        return _parse(_ADDONS.validate_python, data)

    async def list_weekly_board_events(self, guild_id: int, start: datetime, end: datetime) -> list[WeeklyBoardEvent]:
        """Scheduled events starting in ``[start, end)``, soonest first, with their forum thread id and sign-ups."""
        data = await self._request(
            "GET",
            f"{self._guild(guild_id)}/weekly-board/events",
            params={"start": start.isoformat(), "end": end.isoformat()},
        )
        return _parse(_WEEKLY_BOARD_EVENTS.validate_python, data)

    async def set_weekly_board_message(self, guild_id: int, event_id: UUID, message: WeeklyBoardMessage) -> None:
        """Record ``message`` as ``event_id``'s weekly-board card, so the event sync keeps its sign-ups current."""
        await self._request("PUT", f"{self._guild(guild_id)}/weekly-board/events/{event_id}/message", body=message)

    # --- Live status

    async def report_discord_live(self, guild_id: int, report: DiscordLiveReport) -> None:
        """Push the current voice/presence snapshot for ``guild_id``."""
        await self._request("PUT", f"{self._guild(guild_id)}/live", body=report)

    async def put_guild_roles(self, guild_id: int, roles: list[GuildRoleIn]) -> None:
        """Replace ``guild_id``'s full role set (excluding ``@everyone``, dropped by the caller)."""
        await self._request("PUT", f"{self._guild(guild_id)}/roles", body=GuildRolesReplace(roles=roles))

    # --- Attendance

    async def report_voice_presence(self, guild_id: int, report: VoicePresenceReport) -> None:
        """Push cumulative voice minutes per member for one event so far.

        Idempotent: the API keeps ``max(stored, reported)`` per member, so a repeated or restart-reset push never
        lowers the stored total.
        """
        await self._request("PUT", f"{self._guild(guild_id)}/attendance/voice", body=report)

    async def get_voice_presence(self, guild_id: int, event_id: UUID) -> VoicePresenceReport:
        """Currently stored cumulative voice minutes per member for ``event_id``, to resume tracking after a restart."""
        data = await self._request("GET", f"{self._guild(guild_id)}/attendance/voice/{event_id}")
        return _parse(VoicePresenceReport.model_validate, data)

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

    async def confirm_roster_from_notice(
        self, guild_id: int, member_id: int, request: ConfirmByDiscordRequest
    ) -> MemberListing:
        """Confirm site member ``member_id`` on the roster, with the acting GM identified by Discord id."""
        data = await self._request(
            "PUT", f"{self._guild(guild_id)}/signup-notices/{member_id}/roster-confirmation", body=request
        )
        return _parse(MemberListing.model_validate, data)

    async def unconfirm_roster_from_notice(
        self, guild_id: int, member_id: int, request: ConfirmByDiscordRequest
    ) -> MemberListing:
        """Clear site member ``member_id``'s roster confirmation, with the acting GM identified by Discord id."""
        data = await self._request(
            "DELETE", f"{self._guild(guild_id)}/signup-notices/{member_id}/roster-confirmation", body=request
        )
        return _parse(MemberListing.model_validate, data)

    # --- Role resets

    async def list_role_resets(self, guild_id: int) -> list[RoleResetCandidate]:
        """Pending Discord role resets owed after a site account deletion, oldest first, capped at 50."""
        data = await self._request("GET", f"{self._guild(guild_id)}/role-resets")
        return _parse(_ROLE_RESETS.validate_python, data)

    async def ack_role_reset(self, guild_id: int, reset_id: UUID) -> None:
        """Confirm the bot stripped this member's roles (or found them no longer in the guild)."""
        await self._request("POST", f"{self._guild(guild_id)}/role-resets/{reset_id}/ack")

    # --- Badges

    async def sync_badges(self, guild_id: int) -> BadgeSyncReport:
        """Recompute every member's badges, award newly earned ones, and report role state + new announcements."""
        data = await self._request("GET", f"{self._guild(guild_id)}/badges/sync")
        return _parse(BadgeSyncReport.model_validate, data)

    async def ack_badge(self, guild_id: int, ack: BadgeAck) -> None:
        """Confirm one member's badge announcement posted, so ``sync_badges`` stops reporting it as pending."""
        await self._request("POST", f"{self._guild(guild_id)}/badges/ack", body=ack)

    # --- Characters

    async def search_characters(self, query: str | None = None) -> list[CharacterSummary]:
        """The guild roster (``query=None``), or up to 25 stored characters matching ``query``, roster first.

        ``query`` needs 2+ characters (the public ``/api/characters/`` GET's ``q``).
        """
        params: dict[str, Any] = {}
        if query is not None:
            params["q"] = query
        data = await self._request("GET", "/api/characters/", params=params)
        return _parse(_CHARACTER_SUMMARIES.validate_python, data)

    async def get_character(self, slug: str) -> CharacterPage:
        """One character by its ``name-surname`` slug (the public ``/api/characters/{slug}`` GET).

        Raises ``NotFoundError`` if Battle.net has no such character, or ``APIError`` (status 503) if Battle.net
        didn't answer and the character was never stored.
        """
        data = await self._request("GET", f"/api/characters/{slug}")
        return _parse(CharacterPage.model_validate, data)

    async def sync_roster(self, guild_id: int) -> RosterSyncReport:
        """Reconcile the website's character roster with the guild's current Discord/addon data.

        Refreshes up to 8 stale Battle.net profiles per call; safe to call repeatedly. Raises ``NotFoundError`` for
        a non-site ``guild_id``, or ``APIError`` (status 503) if Battle.net is unconfigured or down.
        """
        data = await self._request("POST", f"{self._guild(guild_id)}/characters/sync")
        return _parse(RosterSyncReport.model_validate, data)

    async def get_crafters(
        self, guild_id: int, *, q: str | None = None, profession: str | None = None
    ) -> GuildCrafters:
        """Characters that know a recipe matching ``q`` (recipe or profession substring) / ``profession``.

        Same semantics as the member route (``GET /api/guild/crafters``), but keyed by ``guild_id`` for the bot.
        Raises ``NotFoundError`` for a non-site ``guild_id``, or if the site hasn't deployed this route yet.
        """
        params: dict[str, Any] = {}
        if q is not None:
            params["q"] = q
        if profession is not None:
            params["profession"] = profession
        data = await self._request("GET", f"{self._guild(guild_id)}/crafters", params=params)
        return _parse(GuildCrafters.model_validate, data)

    # --- Character milestones

    async def list_pending_milestones(self, guild_id: int) -> list[CharacterMilestone]:
        """Level 40/60 milestones not yet posted to Discord, oldest first.

        Raises ``NotFoundError`` for a non-site ``guild_id``.
        """
        data = await self._request("GET", f"{self._guild(guild_id)}/characters/milestones/pending")
        return _parse(_PENDING_MILESTONES.validate_python, data)

    async def mark_milestone_posted(self, guild_id: int, milestone_id: UUID, message_id: int | None) -> None:
        """Record that ``milestone_id`` posted to Discord as ``message_id``.

        Raises ``NotFoundError`` for an unknown milestone or a non-site ``guild_id``.
        """
        await self._request(
            "POST",
            f"{self._guild(guild_id)}/characters/milestones/{milestone_id}/posted",
            body=MilestonePosted(discord_message_id=message_id),
        )

    # --- Guild hub announcements

    async def list_pending_announcements(self, guild_id: int) -> list[PendingAnnouncement]:
        """Discord-queued announcements not yet posted, oldest first."""
        data = await self._request("GET", f"{self._guild(guild_id)}/announcements/pending")
        return _parse(PendingAnnouncementsPage.model_validate, data).items

    async def mark_announcement_posted(self, guild_id: int, announcement_id: UUID, message_id: int) -> None:
        """Record that ``announcement_id`` posted to Discord as ``message_id``."""
        await self._request(
            "POST",
            f"{self._guild(guild_id)}/announcements/{announcement_id}/posted",
            body=AnnouncementPosted(message_id=message_id),
        )

    # --- Group voice (LFG)

    async def create_group(self, guild_id: int, create: GroupSessionCreate) -> GroupSessionSchema:
        """Record a group channel the bot just created. Raises ``APIError`` (409) if the channel already has one."""
        data = await self._request("POST", f"{self._guild(guild_id)}/groups", body=create)
        return _parse(GroupSessionSchema.model_validate, data)

    async def list_open_groups(self, guild_id: int) -> list[GroupSessionSchema]:
        """Every group session with no ``ended_at`` yet, oldest first, so a restarted bot can resume counters."""
        data = await self._request("GET", f"{self._guild(guild_id)}/groups/open")
        return _parse(_GROUP_SESSIONS.validate_python, data)

    async def get_group(self, guild_id: int, session_id: UUID) -> GroupSessionSchema:
        """One group session. Raises ``NotFoundError`` if unknown."""
        data = await self._request("GET", f"{self._guild(guild_id)}/groups/{session_id}")
        return _parse(GroupSessionSchema.model_validate, data)

    async def update_group(self, guild_id: int, session_id: UUID, changes: GroupSessionUpdate) -> GroupSessionSchema:
        """Apply only the fields set on ``changes``; ``channel_name=None`` is ignored, never clears it."""
        data = await self._request(
            "PATCH", f"{self._guild(guild_id)}/groups/{session_id}", body=changes, exclude_unset=True
        )
        return _parse(GroupSessionSchema.model_validate, data)

    async def report_group_presence(self, guild_id: int, session_id: UUID, report: GroupPresenceReport) -> None:
        """Push cumulative per-member and per-pair minutes so far; the API keeps ``max(stored, reported)``."""
        await self._request("PUT", f"{self._guild(guild_id)}/groups/{session_id}/presence", body=report)

    async def close_group(self, guild_id: int, session_id: UUID, close: GroupSessionClose) -> None:
        """End a group session with final presence and transcript. Idempotent: closing twice merges presence only."""
        await self._request("POST", f"{self._guild(guild_id)}/groups/{session_id}/close", body=close)

    async def purge_group_transcripts(self, guild_id: int) -> GroupTranscriptPurge:
        """Delete transcripts of sessions ended more than the retention window ago. Called once a day."""
        data = await self._request("POST", f"{self._guild(guild_id)}/groups/purge")
        return _parse(GroupTranscriptPurge.model_validate, data)

    # --- Items

    async def search_items(
        self, query: str, *, page: int = 1, qualities: Sequence[str] = (), slot: str | None = None
    ) -> ItemSearchPage:
        """Items with a whole word of ``query`` in their name (the public ``/api/items`` GET).

        Battle.net matches English word stems (``bless`` finds Blessed, ``thund`` finds nothing); search on submit,
        never as-you-type. 503 if Battle.net isn't configured or didn't answer.
        """
        params: dict[str, Any] = {"q": query, "page": page}
        if qualities:
            params["quality"] = list(qualities)
        if slot is not None:
            params["slot"] = slot
        data = await self._request("GET", "/api/items", params=params)
        return _parse(ItemSearchPage.model_validate, data)

    async def get_item(self, item_id: int) -> Item:
        """One item with its tooltip (the public ``/api/items/{id}`` GET).

        Raises ``NotFoundError`` for an unknown item, or ``APIError`` (status 503) if Battle.net isn't configured
        or didn't answer.
        """
        data = await self._request("GET", f"/api/items/{item_id}")
        return _parse(Item.model_validate, data)

    # --- Achievements

    async def list_achievement_categories(self) -> list[AchievementCategoryTree]:
        """Every root achievement category with its subcategories (the public ``/api/achievements/categories`` GET).

        503 if Battle.net isn't configured or didn't answer.
        """
        data = await self._request("GET", "/api/achievements/categories")
        return _parse(_ACHIEVEMENT_CATEGORIES.validate_python, data)

    async def get_achievement_category(self, category_id: int) -> AchievementCategorySchema:
        """One achievement category and its achievements (the public ``/api/achievements/categories/{id}`` GET).

        Raises ``NotFoundError`` for an unknown category, or ``APIError`` (status 503) if Battle.net isn't
        configured or didn't answer.
        """
        data = await self._request("GET", f"/api/achievements/categories/{category_id}")
        return _parse(AchievementCategorySchema.model_validate, data)

    async def get_achievement(self, achievement_id: int) -> AchievementSchema:
        """One achievement by its game ID (the public ``/api/achievements/{id}`` GET).

        Raises ``NotFoundError`` for an unknown achievement, or ``APIError`` (status 503) if Battle.net isn't
        configured or didn't answer.
        """
        data = await self._request("GET", f"/api/achievements/{achievement_id}")
        return _parse(AchievementSchema.model_validate, data)

    # --- Recipes

    async def get_recipe(self, recipe_id: int) -> RecipeDetail:
        """One recipe by its Battle.net game ID: what it makes, its reagents, and guild crafters.

        The public ``/api/recipes/{id}`` GET. Raises ``NotFoundError`` for an unknown recipe, or ``APIError``
        (status 503) if Battle.net isn't configured or didn't answer.
        """
        data = await self._request("GET", f"/api/recipes/{recipe_id}")
        return _parse(RecipeDetail.model_validate, data)

    # --- Auctions

    async def get_item_price(self, item_id: int) -> ItemAuctionPrice:
        """``item_id``'s current lowest price across the faction's houses, plus a 14-day trend (public).

        Always 200, even for an unknown or unlisted item (``unit_price`` is ``None``, ``houses``/``trend`` empty).
        """
        data = await self._request("GET", f"/api/auctions/prices/{item_id}")
        return _parse(ItemAuctionPrice.model_validate, data)

    async def sync_auctions(self, guild_id: int) -> AuctionSyncReport:
        """Fetch the faction's auction houses, rebuild prices, update history, and queue new alerts.

        Raises ``NotFoundError`` for a non-site ``guild_id``, or ``APIError`` (status 503) if Battle.net didn't
        answer.
        """
        data = await self._request("POST", f"{self._guild(guild_id)}/auctions/sync")
        return _parse(AuctionSyncReport.model_validate, data)

    async def pending_auction_alerts(self, guild_id: int) -> list[AuctionAlertSchema]:
        """Queued price alerts not yet sent, oldest first. Raises ``NotFoundError`` for a non-site ``guild_id``."""
        data = await self._request("GET", f"{self._guild(guild_id)}/auctions/alerts/pending")
        return _parse(_AUCTION_ALERTS.validate_python, data)

    async def ack_auction_alert(self, guild_id: int, alert_id: UUID) -> None:
        """Confirm ``alert_id`` was sent (or couldn't be), so ``pending_auction_alerts`` stops reporting it."""
        await self._request("POST", f"{self._guild(guild_id)}/auctions/alerts/{alert_id}/sent")

    async def list_auction_watches(self, guild_id: int, user_id: int) -> list[AuctionWatch]:
        """``user_id``'s price watches. Raises ``NotFoundError`` if their Discord account isn't linked."""
        data = await self._request("GET", f"{self._guild(guild_id)}/members/{user_id}/auction-watches")
        return _parse(_AUCTION_WATCHES.validate_python, data)

    async def upsert_auction_watch(self, guild_id: int, user_id: int, watch: AuctionWatchCreate) -> AuctionWatch:
        """Add or replace ``user_id``'s watch on ``watch.item_id``.

        Raises ``NotFoundError`` if their Discord account isn't linked, or ``APIError`` (status 409) at
        :data:`MAX_AUCTION_WATCHES` new watches.
        """
        data = await self._request("POST", f"{self._guild(guild_id)}/members/{user_id}/auction-watches", body=watch)
        return _parse(AuctionWatch.model_validate, data)

    async def delete_auction_watch(self, guild_id: int, user_id: int, item_id: int) -> None:
        """Remove ``user_id``'s watch on ``item_id``.

        Raises ``NotFoundError`` if their Discord account isn't linked, or if they have no watch on that item.
        """
        await self._request("DELETE", f"{self._guild(guild_id)}/members/{user_id}/auction-watches/{item_id}")
