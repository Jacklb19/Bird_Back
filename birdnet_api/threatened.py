"""Bird species of the acoustic models that the IUCN Red List classes as threatened (ADR-41).

The list is generated from GBIF by scripts/build_threatened_species.py and shipped with the function, so the map
never asks GBIF per request. A missing or malformed file fails at the first map request, never silently.
"""
import json
from functools import lru_cache
from pathlib import Path
from typing import Final

from .domain import THREATENED_CATEGORIES

THREATENED_SPECIES_FILE: Final = Path(__file__).with_name("threatened_species.json")


@lru_cache(maxsize=1)
def threatened_species() -> frozenset[str]:
    return parse(json.loads(THREATENED_SPECIES_FILE.read_text(encoding="utf-8")))


def parse(document: object) -> frozenset[str]:
    if not isinstance(document, dict) or not isinstance(document.get("species"), dict):
        raise ValueError("The threatened species list must have a `species` object")
    species = document["species"]
    if not all(isinstance(name, str) and category in THREATENED_CATEGORIES for name, category in species.items()):
        raise ValueError("Every threatened species must have one of the categories VU, EN, CR or EW")
    return frozenset(species)
