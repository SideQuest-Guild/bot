"""TTL cache of guild configs so hot paths (every message) don't hit the API."""

import asyncio
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sidequest_bot.api_client import SideQuestAPI
    from sidequest_common.schemas import GuildConfigSchema, GuildConfigUpdate

DEFAULT_TTL_SECONDS = 60.0


class GuildConfigCache:
    """Per-guild config cache.

    Reads are served from memory for ``ttl`` seconds. Writes through :meth:`update` go to the API and replace the
    cached copy immediately, so changes made from slash commands apply without waiting for expiry.
    """

    def __init__(self, api: SideQuestAPI, *, ttl: float = DEFAULT_TTL_SECONDS) -> None:
        self._api = api
        self._ttl = ttl
        self._entries: dict[int, tuple[float, GuildConfigSchema]] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    async def get(self, guild_id: int) -> GuildConfigSchema:
        """Cached config, fetched from the API when missing or stale (one fetch per guild at a time)."""
        if (entry := self._fresh(guild_id)) is not None:
            return entry
        lock = self._locks.setdefault(guild_id, asyncio.Lock())
        async with lock:
            if (entry := self._fresh(guild_id)) is not None:
                return entry
            config = await self._api.get_guild_config(guild_id)
            self._entries[guild_id] = (time.monotonic(), config)
            return config

    async def update(self, guild_id: int, changes: GuildConfigUpdate) -> GuildConfigSchema:
        """Persist ``changes`` and cache the result."""
        config = await self._api.update_guild_config(guild_id, changes)
        self._entries[guild_id] = (time.monotonic(), config)
        return config

    def invalidate(self, guild_id: int) -> None:
        """Drop the cached config so the next read refetches."""
        self._entries.pop(guild_id, None)

    def _fresh(self, guild_id: int) -> GuildConfigSchema | None:
        entry = self._entries.get(guild_id)
        if entry is None or time.monotonic() - entry[0] > self._ttl:
            return None
        return entry[1]
