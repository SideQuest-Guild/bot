import functools
from dataclasses import dataclass, field

import discord
import pytest

from sidequest_bot.lib.roles import configure_block_reason, dangerous_permissions, grant_block_reason


@functools.total_ordering
@dataclass(eq=False)
class FakeRole:
    """Minimal discord.Role stand-in: hierarchy compares by position, like discord.Role."""

    position: int
    permissions: discord.Permissions = field(default_factory=discord.Permissions.none)
    managed: bool = False
    default: bool = False
    assignable: bool = True
    mention: str = "@role"

    def is_default(self) -> bool:
        return self.default

    def is_assignable(self) -> bool:
        return self.assignable

    def __lt__(self, other: FakeRole) -> bool:
        return self.position < other.position

    def __eq__(self, other: object) -> bool:
        return isinstance(other, FakeRole) and self.position == other.position

    def __hash__(self) -> int:
        return hash(self.position)


@dataclass
class FakeGuild:
    owner_id: int = 1


@dataclass
class FakeMember:
    id: int
    top_role: FakeRole
    guild_permissions: discord.Permissions
    guild: FakeGuild = field(default_factory=FakeGuild)


def test_cosmetic_role_can_be_granted() -> None:
    perms = discord.Permissions(send_messages=True, read_messages=True, connect=True, speak=True)
    assert grant_block_reason(FakeRole(position=3, permissions=perms)) is None  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "perm",
    [
        "administrator",
        "manage_roles",
        "manage_guild",
        "ban_members",
        "kick_members",
        "moderate_members",
        "manage_messages",
        "mention_everyone",
        "manage_webhooks",
        "manage_channels",
    ],
)
def test_role_with_any_dangerous_permission_is_never_granted(perm: str) -> None:
    role = FakeRole(position=3, permissions=discord.Permissions(**{perm: True}))
    reason = grant_block_reason(role)  # type: ignore[arg-type]
    assert reason is not None
    assert perm in reason


@pytest.mark.parametrize(
    "role",
    [FakeRole(position=0, default=True), FakeRole(position=3, managed=True), FakeRole(position=9, assignable=False)],
    ids=["everyone", "managed", "above-bot"],
)
def test_structurally_ungrantable_roles(role: FakeRole) -> None:
    assert grant_block_reason(role) is not None  # type: ignore[arg-type]


def test_dangerous_permissions_lists_only_dangerous_bits() -> None:
    perms = discord.Permissions(send_messages=True, ban_members=True, administrator=True)
    assert dangerous_permissions(perms) == ["administrator", "ban_members"]


def _member(top: int, *, manage_roles: bool = True, member_id: int = 2) -> FakeMember:
    return FakeMember(
        id=member_id, top_role=FakeRole(position=top), guild_permissions=discord.Permissions(manage_roles=manage_roles)
    )


def test_configuring_requires_manage_roles() -> None:
    assert configure_block_reason(_member(10, manage_roles=False), FakeRole(position=2)) is not None  # type: ignore[arg-type]


@pytest.mark.parametrize(("role_position", "allowed"), [(4, True), (5, False), (6, False)])
def test_configuring_requires_role_strictly_below_own_top_role(role_position: int, allowed: bool) -> None:
    reason = configure_block_reason(_member(5), FakeRole(position=role_position))  # type: ignore[arg-type]
    assert (reason is None) is allowed


def test_owner_may_configure_any_role() -> None:
    owner = _member(1, manage_roles=False, member_id=1)
    assert configure_block_reason(owner, FakeRole(position=50)) is None  # type: ignore[arg-type]
