"""The nine WoW: Forever classes and the guides the guild recommends for each, shown in every class hall.

Edit ``CLASS_GUIDES`` to change the recommendations; the hall posts update at the next sweep. Each site fills one
row of link buttons, so keep it to five guides per site per class.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Mapping

WOW_CLASSES: Final = ("Druid", "Hunter", "Mage", "Paladin", "Priest", "Rogue", "Shaman", "Warlock", "Warrior")
"""Class names as Blizzard's patch notes and the guild's class roles spell them."""

_METHOD: Final = "https://www.method.gg/wow-forever"
_MOBALYTICS: Final = "https://mobalytics.gg/wow-forever"


@dataclass(frozen=True, slots=True)
class Guide:
    """One recommended guide: the site it's on, what it covers and where it lives."""

    site: str
    label: str
    url: str


def _method(*specs: tuple[str, str], wow_class: str) -> tuple[Guide, ...]:
    """Method's spec guides (``(label, slug)`` under ``/class-guides/``) and its leveling guide for ``wow_class``."""
    leveling_url = f"{_METHOD}/wow-forever-{wow_class.lower()}-leveling-guide-and-talents"
    return (
        *(Guide("Method", label, f"{_METHOD}/class-guides/{slug}") for label, slug in specs),
        Guide("Method", "Leveling", leveling_url),
    )


def _mobalytics(*specs: tuple[str, str], wow_class: str) -> tuple[Guide, ...]:
    """Mobalytics' spec guides (``(label, slug)`` under ``/classes/``) and its page of every ``wow_class`` guide."""
    all_url = f"{_MOBALYTICS}/{wow_class.lower()}-guides"
    return (
        *(Guide("Mobalytics", label, f"{_MOBALYTICS}/classes/{slug}") for label, slug in specs),
        Guide("Mobalytics", f"All {wow_class} guides", all_url),
    )


CLASS_GUIDES: Final[Mapping[str, tuple[Guide, ...]]] = {
    "Druid": (
        *_method(
            ("Balance", "balance-druid"),
            ("Feral (Cat)", "feral-dps-druid"),
            ("Feral (Bear)", "feral-tank-druid"),
            ("Restoration", "restoration-druid"),
            wow_class="Druid",
        ),
        *_mobalytics(
            ("Balance", "balance-druid-guide"),
            ("Feral (Cat)", "feral-druid-guide"),
            ("Feral (Bear)", "feral-druid-tank-guide"),
            ("Restoration", "restoration-druid-guide-progress"),
            wow_class="Druid",
        ),
    ),
    "Hunter": (
        *_method(
            ("Beast Mastery", "beast-mastery-hunter"),
            ("Marksmanship", "marksmanship-hunter"),
            ("Survival", "survival-hunter"),
            wow_class="Hunter",
        ),
        *_mobalytics(
            ("Beast Mastery", "beast-mastery-hunter-guide-progress"),
            ("Marksmanship", "marksmanship-hunter-guide"),
            wow_class="Hunter",
        ),
    ),
    "Mage": (
        *_method(("Arcane", "arcane-mage"), ("Fire", "fire-mage"), ("Frost", "frost-mage"), wow_class="Mage"),
        *_mobalytics(("Arcane", "arcane-mage-guide-progress"), ("Frost", "frost-mage-guide"), wow_class="Mage"),
    ),
    "Paladin": (
        *_method(
            ("Holy", "holy-paladin"),
            ("Protection", "protection-paladin"),
            ("Retribution", "retribution-paladin"),
            wow_class="Paladin",
        ),
        *_mobalytics(
            ("Protection", "protection-paladin-guide"),
            ("Retribution", "retribution-paladin-guide"),
            wow_class="Paladin",
        ),
    ),
    "Priest": (
        *_method(("Holy", "holy-priest"), ("Shadow", "shadow-priest"), wow_class="Priest"),
        *_mobalytics(
            ("Discipline", "discipline-priest-guide-progress"),
            ("Shadow", "shadow-priest-guide-progress"),
            wow_class="Priest",
        ),
    ),
    "Rogue": (
        *_method(
            ("Assassination", "assassination-rogue"),
            ("Combat", "combat-rogue"),
            ("Subtlety", "subtlety-rogue"),
            wow_class="Rogue",
        ),
        *_mobalytics(wow_class="Rogue"),
    ),
    "Shaman": (
        *_method(
            ("Elemental", "elemental-shaman"),
            ("Enhancement", "enhancement-shaman"),
            ("Restoration", "restoration-shaman"),
            wow_class="Shaman",
        ),
        *_mobalytics(
            ("Elemental", "elemental-shaman-guide"),
            ("Enhancement", "enhancement-shaman-guide"),
            ("Restoration", "restoration-shaman-guide"),
            wow_class="Shaman",
        ),
    ),
    "Warlock": (
        *_method(
            ("Affliction", "affliction-warlock"),
            ("Demonology", "demonology-warlock"),
            ("Destruction", "destruction-warlock"),
            wow_class="Warlock",
        ),
        *_mobalytics(
            ("Affliction", "affliction-warlock-guide"),
            ("Demonology", "demonology-warlock-guide"),
            ("Destruction", "destruction-warlock-guide"),
            wow_class="Warlock",
        ),
    ),
    "Warrior": (
        *_method(
            ("Arms", "arms-warrior"),
            ("Fury", "fury-warrior"),
            ("Protection", "protection-warrior"),
            wow_class="Warrior",
        ),
        *_mobalytics(("Protection", "protection-warrior-guide-progress"), wow_class="Warrior"),
    ),
}
