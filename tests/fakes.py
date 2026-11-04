"""A fake Discord server for tests and evals: enough guild for the AI helper to look at, plan against and change.

Objects are ``MagicMock``s with the real classes as spec, so ``isinstance`` checks pass. Creating roles and channels,
setting permissions and adding roles change the fake's state, so a check after a plan runs sees what Discord would.
"""

from collections.abc import Iterable
from itertools import count
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord

_ids = count(900_000)

ChannelSpec = type[discord.abc.GuildChannel]


def make_role(role_id: int, name: str, *, permissions: discord.Permissions | None = None) -> MagicMock:
    """A role; ``@everyone`` when ``name`` is ``"@everyone"``."""
    role = MagicMock(spec=discord.Role)
    role.id = role_id
    role.name = name
    role.mention = "@everyone" if name == "@everyone" else f"<@&{role_id}>"
    role.members = []
    role.managed = False
    role.permissions = permissions or discord.Permissions.none()
    role.is_default.return_value = name == "@everyone"
    role.is_assignable.return_value = True
    return role


def make_channel(
    spec: ChannelSpec,
    channel_id: int,
    name: str,
    overwrites: dict[Any, discord.PermissionOverwrite] | None = None,
    *,
    category: MagicMock | None = None,
) -> MagicMock:
    """A channel or category. With ``overwrites=None`` in a category, it is synced to the category's access."""
    channel = MagicMock(spec=spec)
    channel.id = channel_id
    channel.name = name
    channel.mention = f"<#{channel_id}>"
    channel.category = category
    channel.category_id = category.id if category is not None else None
    channel.overwrites = (
        dict(category.overwrites) if overwrites is None and category is not None else (overwrites or {})
    )
    channel.permissions_synced = category is not None and channel.overwrites == category.overwrites
    channel.overwrites_for.side_effect = lambda target: channel.overwrites.get(target, discord.PermissionOverwrite())

    async def set_permissions(target: Any, *, overwrite: discord.PermissionOverwrite, **_kwargs: Any) -> None:
        channel.overwrites[target] = overwrite
        channel.permissions_synced = False

    async def send(content: str, **_kwargs: Any) -> MagicMock:
        message = MagicMock(spec=discord.Message)
        message.content = content
        message.jump_url = f"https://discord.com/channels/1/{channel_id}/{next(_ids)}"
        return message

    channel.set_permissions = AsyncMock(side_effect=set_permissions)
    channel.send = AsyncMock(side_effect=send)
    return channel


def make_member(member_id: int, name: str, roles: list[MagicMock], **permissions: bool) -> MagicMock:
    """A member holding ``roles`` (put ``@everyone`` first, as Discord does) with these guild permissions."""
    member = MagicMock(spec=discord.Member)
    member.id = member_id
    member.name = name
    member.display_name = name.title()
    member.global_name = None
    member.mention = f"<@{member_id}>"
    member.bot = False
    member.roles = roles
    member.top_role = roles[-1]
    member.guild_permissions = discord.Permissions(**permissions)
    member.get_role.side_effect = lambda role_id: next((r for r in member.roles if r.id == role_id), None)
    for role in roles:
        role.members.append(member)

    async def add_roles(*added: MagicMock, **_kwargs: Any) -> None:
        member.roles.extend(added)

    member.add_roles = AsyncMock(side_effect=add_roles)
    return member


def make_guild(roles: list[MagicMock], channels: list[MagicMock], members: list[MagicMock]) -> MagicMock:
    """A guild whose create methods add real-looking objects to it. ``roles[0]`` must be ``@everyone``."""
    guild = MagicMock(spec=discord.Guild)
    guild.id = 1
    guild.owner_id = 1
    guild.roles = roles
    guild.channels = channels
    guild.members = members
    guild.default_role = roles[0]
    guild.self_role = None  # the bot's managed role; tests that need one set it
    guild.get_role.side_effect = lambda role_id: next((r for r in guild.roles if r.id == role_id), None)
    guild.get_channel.side_effect = lambda cid: next((c for c in guild.channels if c.id == cid), None)
    guild.get_member.side_effect = lambda mid: next((m for m in guild.members if m.id == mid), None)

    def by_category() -> list[tuple[MagicMock | None, list[MagicMock]]]:
        categories = [c for c in guild.channels if isinstance(c, discord.CategoryChannel)]
        loose = [c for c in guild.channels if c.category is None and c not in categories]
        grouped = [(cat, [c for c in guild.channels if c.category is cat]) for cat in categories]
        return [(None, loose), *grouped] if loose else grouped

    guild.by_category.side_effect = by_category
    for member in members:
        member.guild = guild

    async def create_role(**kwargs: Any) -> MagicMock:
        role = make_role(next(_ids), kwargs["name"], permissions=kwargs.get("permissions"))
        role.created_with = kwargs
        guild.roles.insert(1, role)  # just above @everyone, like Discord
        return role

    def creator(spec: ChannelSpec) -> AsyncMock:
        async def create(name: str, **kwargs: Any) -> MagicMock:
            channel = make_channel(
                spec, next(_ids), name, dict(kwargs.get("overwrites") or {}), category=kwargs.get("category")
            )
            channel.created_with = kwargs
            guild.channels.append(channel)
            return channel

        return AsyncMock(side_effect=create)

    guild.create_role = AsyncMock(side_effect=create_role)
    guild.create_text_channel = creator(discord.TextChannel)
    guild.create_voice_channel = creator(discord.VoiceChannel)
    guild.create_category = creator(discord.CategoryChannel)
    return guild


def named(items: Iterable[MagicMock], name: str) -> MagicMock:
    """The one item called ``name`` (case-insensitive)."""
    matches = [item for item in items if item.name.casefold() == name.casefold()]
    if len(matches) != 1:
        msg = f"expected one item called {name}, found {len(matches)}"
        raise LookupError(msg)
    return matches[0]


def can_see(channel: MagicMock, roles: Iterable[MagicMock]) -> bool:
    """Whether someone holding ``roles`` (including ``@everyone``) can see ``channel``, by Discord's overwrite rules.

    Assumes ``@everyone`` may view channels server-wide, which is Discord's default.
    """
    roles = list(roles)
    if any(role.permissions.administrator for role in roles):
        return True
    visible = True
    for target, overwrite in channel.overwrites.items():
        if getattr(target, "is_default", None) and target.is_default() and overwrite.view_channel is not None:
            visible = overwrite.view_channel
    role_rules = [
        overwrite.view_channel
        for target, overwrite in channel.overwrites.items()
        if target in roles and not target.is_default() and overwrite.view_channel is not None
    ]
    if True in role_rules:
        return True
    if False in role_rules:
        return False
    return visible


def _hidden_except(everyone: MagicMock, *roles: MagicMock) -> dict[Any, discord.PermissionOverwrite]:
    access = {everyone: discord.PermissionOverwrite(view_channel=False)}
    access.update({role: discord.PermissionOverwrite(view_channel=True) for role in roles})
    return access


CLASSES = ("Warrior", "Paladin", "Hunter", "Rogue", "Priest", "Shaman", "Mage", "Warlock", "Druid")
LEVELS = ("Lv 5 · Wayfarer", "Lv 10 · Adventurer", "Lv 20 · Veteran", "Lv 30 · Champion", "Lv 50 · Legend")
BADGES = ("First Steps", "Raid Regular", "Loot Goblin")


def sidequest_server(*, extra_roles: Iterable[str] = ()) -> MagicMock:
    """The SideQuest Discord's layout as of November 2026: its real channels, ranks, classes, levels and badges.

    Guests (``At the Gate``) see the rules, news and sign-in; signed-in guests also see the Guest Hall; confirmed
    members (``New Arrival``) see the SideQuest and Groups categories; Quest Guides and Quest Masters see the
    Officers' Quarters. ``extra_roles`` adds plain roles, such as one a test expects to already exist.
    """
    ids = count(1)
    everyone = make_role(next(ids), "@everyone")
    gm_powers = discord.Permissions(administrator=True)
    guide_powers = discord.Permissions(manage_messages=True, moderate_members=True, kick_members=True)
    quest_master = make_role(next(ids), "Quest Master", permissions=gm_powers)
    quest_guide = make_role(next(ids), "Quest Guide", permissions=guide_powers)
    new_arrival = make_role(next(ids), "New Arrival")
    signed = make_role(next(ids), "Signed the Ledger")
    tagged = make_role(next(ids), "Battle-Tagged")
    gate = make_role(next(ids), "At the Gate")
    away = make_role(next(ids), "Away on a Quest")
    familiar = make_role(next(ids), "Familiar")
    familiar.managed = True
    plain = [make_role(next(ids), name) for name in (*CLASSES, *LEVELS, *BADGES, *extra_roles)]
    # Lowest first, as discord.py orders guild.roles.
    roles = [everyone, gate, *plain, away, tagged, signed, new_arrival, familiar, quest_guide, quest_master]

    channel_ids = count(1000)

    def category(name: str, access: dict[Any, discord.PermissionOverwrite]) -> MagicMock:
        return make_channel(discord.CategoryChannel, next(channel_ids), name, access)

    def channel(spec: ChannelSpec, name: str, parent: MagicMock | None = None) -> MagicMock:
        return make_channel(spec, next(channel_ids), name, category=parent)

    sidequest = category("SideQuest", _hidden_except(everyone, new_arrival))
    groups = category("Groups", _hidden_except(everyone, new_arrival))
    officers = category("Officers' Quarters", _hidden_except(everyone, quest_guide, quest_master))
    guest_hall = category("Guest Hall", _hidden_except(everyone, signed, new_arrival))
    channels = [
        channel(discord.TextChannel, "rules"),
        channel(discord.TextChannel, "news"),
        channel(discord.TextChannel, "feed"),
        channel(discord.TextChannel, "sign-the-ledger"),
        sidequest,
        channel(discord.TextChannel, "general", sidequest),
        channel(discord.ForumChannel, "events", sidequest),
        channel(discord.TextChannel, "choose-your-class", sidequest),
        channel(discord.ForumChannel, "class-halls", sidequest),
        channel(discord.VoiceChannel, "The Tavern", sidequest),
        groups,
        channel(discord.VoiceChannel, "raid-1", groups),
        channel(discord.VoiceChannel, "raid-2", groups),
        channel(discord.VoiceChannel, "dungeon-1", groups),
        channel(discord.VoiceChannel, "dungeon-2", groups),
        channel(discord.VoiceChannel, "dungeon-3", groups),
        channel(discord.StageChannel, "Events", groups),
        channel(discord.VoiceChannel, "AFK 🪑", groups),
        officers,
        channel(discord.TextChannel, "officer-chat", officers),
        channel(discord.TextChannel, "moderator-only", officers),
        channel(discord.TextChannel, "recruitment", officers),
        channel(discord.TextChannel, "guild-log", officers),
        channel(discord.TextChannel, "platform", officers),
        channel(discord.VoiceChannel, "Officer Voice", officers),
        guest_hall,
        channel(discord.TextChannel, "guest-chat", guest_hall),
        channel(discord.VoiceChannel, "Lobby", guest_hall),
    ]
    warrior, mage = named(plain, "Warrior"), named(plain, "Mage")
    members = [
        make_member(201, "coffee", [everyone, new_arrival, quest_master], administrator=True),
        make_member(202, "zach", [everyone, new_arrival, quest_master], administrator=True),
        make_member(203, "lyra", [everyone, warrior, new_arrival, quest_guide]),
        make_member(204, "alex", [everyone, mage, new_arrival]),
        make_member(205, "wanderer", [everyone, signed]),
        make_member(206, "stranger", [everyone, gate]),
    ]
    return make_guild(roles, channels, members)
