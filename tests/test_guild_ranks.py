"""In-game guild rank -> Discord rank role mapping (``sidequest_bot.lib.guild_ranks``)."""

import pytest

from sidequest_bot.lib.guild_ranks import rank_role_name, wanted_rank_role


@pytest.mark.parametrize(
    ("rank", "role"),
    [
        ("Adventurer", "Adventurer"),
        ("new arrival", "New Arrival"),
        ("Expedition Lead", "Expedition Leader"),  # the game cuts rank names at 15 characters
        ("Seasoned Advent", "Seasoned Adventurer"),
        ("Quest MasterAlt", "Alternate Path"),  # an officer's alt is an alt, not an officer
        ("Alternate Path", "Alternate Path"),
        ("Quest", None),  # starts two roles' names: ambiguous, so nothing
        ("Raider", None),
        ("", None),
    ],
)
def test_rank_role_name(rank: str, role: str | None) -> None:
    assert rank_role_name(rank) == role


def test_highest_rank_wins_and_an_alt_never_outranks_a_main() -> None:
    assert wanted_rank_role(["Quest MasterAlt", "Adventurer", "New Arrival"], []) == "Adventurer"


def test_an_officer_rank_counts_only_from_a_battlenet_owned_character() -> None:
    assert wanted_rank_role(["Quest Guide", "Adventurer"], ["Quest Guide", "Adventurer"]) == "Quest Guide"
    # The officer character is only linked on the site: leave their rank roles alone, neither grant nor demote.
    assert wanted_rank_role(["Quest Guide", "Adventurer"], ["Adventurer"]) is None


def test_no_mappable_rank_means_leave_alone() -> None:
    assert wanted_rank_role([], []) is None
    assert wanted_rank_role(["Raider"], ["Raider"]) is None
