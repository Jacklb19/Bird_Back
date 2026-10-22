"""Write birdnet_api/threatened_species.json: the birds of the acoustic models that the IUCN Red List classes VU, EN,
CR or EW, according to GBIF (ADR-41). Run it again when the models' labels or the Red List change:

    python scripts/build_threatened_species.py ../frontend/public/models/labels.txt ../frontend/public/models/birdnet_plus_labels.txt

A label counts when GBIF matches it exactly as a bird species (synonyms follow their accepted species, the same rule
the API uses for species cards) and GBIF's copy of the global IUCN Red List gives that species one of the categories.
GBIF's species search by threat status is not used: it mixes in national red lists, which would hide birds that are
only threatened in one country.
"""
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import httpx

PROJECT: Final = Path(__file__).resolve().parents[1]
OUTPUT: Final = PROJECT / "birdnet_api" / "threatened_species.json"
GBIF_API_URL: Final = "https://api.gbif.org/v1"
THREATENED: Final = ("VU", "EN", "CR", "EW")
NO_CONTENT: Final = 204
# Parallel requests; GBIF asks for moderate automated use.
WORKERS: Final = 6
TIMEOUT_SECONDS: Final = 30.0
USER_AGENT: Final = "Trino/1.0 (https://birdnet-nu.vercel.app; bird sound app of a university project)"
SOURCE: Final = "IUCN Red List category of each species in GBIF (The IUCN Red List of Threatened Species dataset on GBIF, CC BY 4.0)"
LABEL_SEPARATOR: Final = "_"


def category(client: httpx.Client, name: str) -> str | None:
    """Global IUCN category of a label, None when GBIF has no exact bird species for it; "" when it has no category."""
    match = client.get(f"{GBIF_API_URL}/species/match", params={"name": name, "kingdom": "Animalia", "class": "Aves"})
    match.raise_for_status()
    body = match.json()
    key = body.get("acceptedUsageKey", body.get("usageKey"))
    if body.get("matchType") != "EXACT" or body.get("rank") != "SPECIES" or body.get("class") != "Aves" or not isinstance(key, int):
        return None
    response = client.get(f"{GBIF_API_URL}/species/{key}/iucnRedListCategory")
    if response.status_code == NO_CONTENT:
        return ""
    response.raise_for_status()
    code = response.json().get("code")
    return code if isinstance(code, str) else ""


def label_names(paths: list[Path]) -> list[str]:
    names = {line.split(LABEL_SEPARATOR, 1)[0].strip() for path in paths for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()}
    # Only binomials are species; the models' other classes (engine, dog, human voice) are not.
    return sorted(name for name in names if len(name.split()) == 2 and name[0].isupper())


def main(arguments: list[str]) -> None:
    if not arguments:
        raise SystemExit(__doc__)
    names = label_names([Path(argument) for argument in arguments])
    with httpx.Client(timeout=TIMEOUT_SECONDS, headers={"User-Agent": USER_AGENT}) as client, ThreadPoolExecutor(max_workers=WORKERS) as pool:
        categories = dict(zip(names, pool.map(lambda name: category(client, name), names), strict=True))
    species = {name: code for name, code in categories.items() if code in THREATENED}
    unmatched = sum(code is None for code in categories.values())
    document = {
        "source": SOURCE,
        "generated_at": datetime.now(UTC).date().isoformat(),
        "labels": len(names),
        "unmatched_labels": unmatched,
        "species": dict(sorted(species.items())),
    }
    OUTPUT.write_text(json.dumps(document, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    counts = {code: sum(value == code for value in species.values()) for code in THREATENED}
    print(f"{len(species)} threatened of {len(names)} labels ({unmatched} without an exact GBIF match): {counts}")


if __name__ == "__main__":
    main(sys.argv[1:])
