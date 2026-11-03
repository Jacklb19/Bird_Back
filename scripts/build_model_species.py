"""Write birdnet_api/model_species.json: the scientific names of every species the acoustic models can name. Public
routes that ask outside sources about a species (its reference sounds, a new card) answer only for these, so a made-up
name neither slips past the CDN cache nor spends the keys of those sources. Run it again when the models' labels change:

    python scripts/build_model_species.py ../frontend/public/models/labels.txt ../frontend/public/models/birdnet_plus_labels.txt

Each label reads `Scientific name_Common name`; only binomials are species, the models' other classes (engine, dog,
human voice) are not. The same rule picks the labels of scripts/build_threatened_species.py.
"""
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

PROJECT: Final = Path(__file__).resolve().parents[1]
OUTPUT: Final = PROJECT / "birdnet_api" / "model_species.json"
SOURCE: Final = "Labels of the BirdNET v2.4 and BirdNET+ V3 models served to the web app"
LABEL_SEPARATOR: Final = "_"


def label_names(paths: list[Path]) -> list[str]:
    names = {line.split(LABEL_SEPARATOR, 1)[0].strip() for path in paths for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()}
    return sorted(name for name in names if len(name.split()) == 2 and name[0].isupper())


def main(arguments: list[str]) -> None:
    if not arguments:
        raise SystemExit(__doc__)
    names = label_names([Path(argument) for argument in arguments])
    document = {"source": SOURCE, "generated_at": datetime.now(UTC).date().isoformat(), "species": names}
    OUTPUT.write_text(json.dumps(document, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"{len(names)} species written to {OUTPUT.relative_to(PROJECT)} ({OUTPUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main(sys.argv[1:])
