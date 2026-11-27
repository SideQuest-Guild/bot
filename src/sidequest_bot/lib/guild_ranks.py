"""Guild rank roles: which Discord rank role a member's in-game guild rank calls for.

The API reports the in-game rank names a member's characters hold on the Battle.net guild roster
(``MemberLink.guild_ranks``, best first). Each name maps to one Discord rank role:

* the role with the same name (case-insensitive): ``Adventurer`` -> **Adventurer**;
* else the one role whose name starts with it, since the game cuts rank names at 15 characters:
  ``Expedition Lead`` -> **Expedition Leader**;
* else, for a rank ending in ``Alt`` whose stem is a rank (``Quest MasterAlt``), **Alternate Path**: an alt's rank says
  whose alt it is, not what the player is, and their main's rank wins anyway.

A name that matches nothing is ignored. The member gets the highest rank role any of their ranks maps to, and loses
every other rank role. The three officer roles carry moderator permissions, so they're only granted from
``battlenet_guild_ranks``: characters the member's own Battle.net account owns, which no addon upload or site link
can fake. When a member's highest rank is an officer rank that only a site-linked character holds, the sync leaves
their rank roles alone rather than grant it, or demote someone who really is an officer.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

RANK_ROLE_NAMES: tuple[str, ...] = (
    "Quest Master",
    "Quest Guide",
    "Expedition Leader",
    "Seasoned Adventurer",
    "Adventurer",
    "New Arrival",
    "Alternate Path",
)
"""Every Discord guild rank role, highest first."""

OFFICER_RANK_ROLE_NAMES = frozenset({"Quest Master", "Quest Guide", "Expedition Leader"})
"""Rank roles with moderator permissions: granted only from Battle.net-owned characters."""

ALT_RANK_ROLE_NAME = "Alternate Path"

_ALT_SUFFIX = "alt"


def _match(rank: str) -> str | None:
    """The rank role ``rank`` names exactly or as a cut-off prefix, or None."""
    key = rank.strip().casefold()
    if not key:
        return None
    for name in RANK_ROLE_NAMES:
        if name.casefold() == key:
            return name
    prefixed = [name for name in RANK_ROLE_NAMES if name.casefold().startswith(key)]
    return prefixed[0] if len(prefixed) == 1 else None


def rank_role_name(rank: str) -> str | None:
    """The Discord rank role for the in-game rank ``rank``, or None if it maps to none (see the module docstring)."""
    if (name := _match(rank)) is not None:
        return name
    stem = rank.strip()
    if stem.casefold().endswith(_ALT_SUFFIX) and _match(stem[: -len(_ALT_SUFFIX)]) is not None:
        return ALT_RANK_ROLE_NAME
    return None


def wanted_rank_role(ranks: Sequence[str], battlenet_ranks: Sequence[str]) -> str | None:
    """The one rank role a member should hold, or None to leave their rank roles as they are.

    ``ranks`` are all their characters' in-game ranks, ``battlenet_ranks`` those of the characters their Battle.net
    account owns. None when no rank maps to a rank role, or when the highest is an officer role no Battle.net-owned
    character's rank calls for.
    """
    candidates = {name for rank in ranks if (name := rank_role_name(rank)) is not None}
    best = next((name for name in RANK_ROLE_NAMES if name in candidates), None)
    if best in OFFICER_RANK_ROLE_NAMES and best not in {rank_role_name(rank) for rank in battlenet_ranks}:
        return None
    return best
