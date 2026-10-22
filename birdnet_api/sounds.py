"""Reference recordings of a species from xeno-canto (ADR-42), so a person can hear how it sings in its card.

The key of the API stays here; the phone gets only what xeno-canto's terms ask to show with each recording
(recordist, licence, catalogue number and link) and plays the file from xeno-canto itself. Locations are dropped:
the card needs none, and xeno-canto already withholds file and place for species vulnerable to capture.
"""
import re
from typing import Any, Final
from urllib.parse import urlparse

import httpx

from .contracts import ReferenceSound, ReferenceSounds, SoundLicense
from .errors import ErrorCode, raise_error
from .public_sources import USER_AGENT
from .settings import get_settings

SOURCE: Final = "xeno-canto"
# Quality A or B and under a minute: the file is downloaded whole (xeno-canto serves no byte ranges), so a short,
# clean recording is what plays well on a phone.
QUERY_TEMPLATE: Final = 'sp:"{species}" grp:birds q:">C" len:"<60"'
# The smallest page xeno-canto's API v3 accepts.
PAGE_SIZE: Final = 50
MAX_SOUNDS: Final = 3
RECORDING_PAGE: Final = "https://xeno-canto.org/{id}"
RECORDING_FILE: Final = "https://xeno-canto.org/{id}/download"
CATALOGUE_PREFIX: Final = "XC"
CATALOGUE_NUMBER: Final = re.compile(r"\d{1,9}")
LENGTH: Final = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})")
# Path of a Creative Commons licence URL: /licenses/by-nc-sa/4.0/ or /publicdomain/zero/1.0/.
LICENSE_PATH: Final = re.compile(r"/(licenses|publicdomain)/([a-z-]+)/(\d(?:\.\d)?)/?")
LICENSE_HOST: Final = "creativecommons.org"
SONG: Final = "song"
QUALITY_ORDER: Final = ("A", "B")


def reference_sounds(species: str) -> ReferenceSounds:
    settings = get_settings()
    if not settings.sounds_api_key:
        raise_error(ErrorCode.SOUNDS_NOT_CONFIGURED)
    params = {"query": QUERY_TEMPLATE.format(species=species), "key": settings.sounds_api_key, "per_page": PAGE_SIZE}
    try:
        with httpx.Client(timeout=settings.public_data_timeout_seconds, headers={"User-Agent": USER_AGENT}) as client:
            response = client.get(f"{settings.sounds_api_url}/recordings", params=params)
            response.raise_for_status()
            body = response.json()
    except (httpx.HTTPError, ValueError):
        raise_error(ErrorCode.SOUNDS_UNAVAILABLE)
    return choose(species, body)


def choose(species: str, body: object) -> ReferenceSounds:
    """The best few recordings of the answer: quality first, songs before calls, then shorter ones."""
    recordings = body.get("recordings") if isinstance(body, dict) else None
    if not isinstance(recordings, list):
        raise_error(ErrorCode.SOUNDS_UNAVAILABLE)
    found = [recording for recording in recordings if isinstance(recording, dict) and _is_species(recording, species)]
    sounds = [sound for sound in map(parse, found) if sound is not None]
    sounds.sort(key=lambda sound: (QUALITY_ORDER.index(sound.quality), SONG not in sound.type, sound.length_seconds))
    # Found but without any file to play: xeno-canto redacts the recordings of restricted species.
    restricted = bool(found) and not sounds and any(_is_redacted(recording) for recording in found)
    return ReferenceSounds(species=species, source=SOURCE, restricted=restricted, sounds=sounds[:MAX_SOUNDS])


def parse(recording: dict[str, Any]) -> ReferenceSound | None:
    """A recording with everything its licence asks to show, or None when anything is missing or unexpected."""
    number, recordist, quality = recording.get("id"), recording.get("rec"), recording.get("q")
    if not isinstance(number, str) or not CATALOGUE_NUMBER.fullmatch(number) or _is_redacted(recording):
        return None
    if not isinstance(recordist, str) or not recordist.strip() or quality not in QUALITY_ORDER:
        return None
    license_ = parse_license(recording.get("lic"))
    length = parse_length(recording.get("length"))
    if license_ is None or length is None:
        return None
    sound_type = recording.get("type")
    country = recording.get("cnt")
    return ReferenceSound(
        id=f"{CATALOGUE_PREFIX}{number}", type=sound_type.strip().lower() if isinstance(sound_type, str) else "", length_seconds=length,
        country=country.strip() if isinstance(country, str) and country.strip() else None, recordist=recordist.strip(),
        license=license_, quality=quality, page_url=RECORDING_PAGE.format(id=number), audio_url=RECORDING_FILE.format(id=number))


def parse_license(value: object) -> SoundLicense | None:
    """Creative Commons licence of a recording as "CC BY-NC-SA 4.0" and its canonical URL."""
    if not isinstance(value, str):
        return None
    url = urlparse(value if not value.startswith("//") else f"https:{value}")
    match = LICENSE_PATH.fullmatch(url.path)
    if url.hostname != LICENSE_HOST or match is None:
        return None
    kind, terms, version = match.groups()
    if kind == "publicdomain" and terms != "zero":
        return None
    code = f"CC0 {version}" if kind == "publicdomain" else f"CC {terms.upper()} {version}"
    return SoundLicense(code=code, url=f"https://{LICENSE_HOST}/{kind}/{terms}/{version}/")


def parse_length(value: object) -> int | None:
    match = LENGTH.fullmatch(value.strip()) if isinstance(value, str) else None
    if match is None:
        return None
    hours, minutes, seconds = match.groups()
    total = int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds)
    return total if total > 0 else None


def _is_species(recording: dict[str, Any], species: str) -> bool:
    genus, epithet = recording.get("gen"), recording.get("sp")
    return isinstance(genus, str) and isinstance(epithet, str) and f"{genus} {epithet}".lower() == species.lower()


def _is_redacted(recording: dict[str, Any]) -> bool:
    meta = recording.get("_meta")
    redacted = meta.get("redacted_fields") if isinstance(meta, dict) else None
    return (isinstance(redacted, dict) and "file" in redacted) or not recording.get("file")
