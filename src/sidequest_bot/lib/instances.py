"""WoW: Forever's dungeons and raids, as group owners pick them for their voice channel's status.

Dungeons are split at level 37 because a Discord select holds at most 25 options; each list stays in level order.
New instances go here by hand when Blizzard adds them.
"""

from typing import Final

DUNGEONS_LOW: Final = (
    "Ragefire Chasm",
    "Hall of Thanes",
    "Ruins of Lordaeron",
    "The Deadmines",
    "Wailing Caverns",
    "Shadowfang Keep",
    "Blackfathom Deeps",
    "Excavation Site: Wetlands",
    "The Stockade",
    "City of Dalaran",
    "Gnomeregan",
    "Razorfen Kraul",
    "Scarlet Monastery: Graveyard",
    "Scarlet Monastery: Library",
    "Scarlet Monastery: Armory",
    "Scarlet Monastery: Cathedral",
    "The Drowned City",
)
"""Dungeons for levels 13 to 40."""

DUNGEONS_HIGH: Final = (
    "Uldaman",
    "Razorfen Downs",
    "Zul'Farrak",
    "Krol'dok Stronghold",
    "Maraudon",
    "Alcaz Prison",
    "Sunken Temple",
    "Blackrock Depths",
    "Blackmaw Hold",
    "Lower Blackrock Spire",
    "Upper Blackrock Spire",
    "Shaper's Terrace",
    "Dire Maul: East",
    "Dire Maul: West",
    "Dire Maul: North",
    "Stratholme",
    "Scholomance",
)
"""Dungeons for levels 37 to 60."""

RAIDS: Final = ("Hyjal Summit", "Barrow Deeps", "Onyxia's Lair")
