"""Scientific names of every species the acoustic models can name.

Generated from the models' labels by scripts/build_model_species.py and shipped with the function. A missing or
malformed file fails at the first request that needs it, never silently.
"""
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Final

from .domain import SCIENTIFIC_NAME_PATTERN
from .errors import ErrorCode, raise_error

MODEL_SPECIES_FILE: Final = Path(__file__).with_name("model_species.json")
SCIENTIFIC_NAME: Final = re.compile(SCIENTIFIC_NAME_PATTERN)


@lru_cache(maxsize=1)
def model_species() -> frozenset[str]:
    return parse(json.loads(MODEL_SPECIES_FILE.read_text(encoding="utf-8")))


def parse(document: object) -> frozenset[str]:
    if not isinstance(document, dict) or not isinstance(document.get("species"), list) or not document["species"]:
        raise ValueError("The model species list must have a non-empty `species` array")
    species = document["species"]
    if not all(isinstance(name, str) and SCIENTIFIC_NAME.fullmatch(name) for name in species):
        raise ValueError("Every model species must be a scientific name")
    return frozenset(species)


def require_model_species(species: str) -> None:
    """Refuses a name the models cannot detect before anything asks an outside source about it."""
    if species not in model_species():
        raise_error(ErrorCode.SPECIES_NOT_IN_MODELS)
