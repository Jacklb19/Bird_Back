"""Public facts about a species, gathered by the API itself for its card (RF-14): the client only names the species.

The same sources the app already shows: GBIF (classification and IUCN Red List category) and the lead of the
Wikipedia article. A name GBIF does not know exactly as a bird species is refused, so nothing but a bird name ever
reaches the language model.
"""
import logging
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import quote

import httpx

from .domain import CardLanguage
from .errors import ErrorCode, raise_error
from .settings import get_settings

GBIF_API_URL: Final = "https://api.gbif.org/v1"
GBIF_SPECIES_PAGE: Final = "https://www.gbif.org/species/{key}"
WIKIPEDIA_SUMMARY_URL: Final = "https://{language}.wikipedia.org/api/rest_v1/page/summary/{title}"
WIKIPEDIA_LICENSE: Final = "CC BY-SA 4.0"
GBIF_LICENSE: Final = "CC BY 4.0"
# Wikimedia asks automated clients to identify themselves.
USER_AGENT: Final = "Trino/1.0 (https://birdnet-nu.vercel.app; bird sound app of a university project)"
# English is the fallback when the article does not exist in the requested language, as in the app.
FALLBACK_LANGUAGE: Final = CardLanguage.EN
# A disambiguation page describes no single subject.
DISAMBIGUATION: Final = "disambiguation"
# The lead of an article is a few hundred characters; this only bounds what is passed on.
MAX_EXTRACT_LENGTH: Final = 3000
IUCN_CODES: Final = frozenset({"LC", "NT", "VU", "EN", "CR", "EW", "EX", "DD", "NE"})
NO_CONTENT: Final = 204
NOT_FOUND: Final = 404

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Source:
    name: str
    url: str
    license: str


@dataclass(frozen=True, slots=True)
class SpeciesFacts:
    scientific_name: str
    order: str
    family: str
    genus: str
    iucn_category: str | None
    # Lead of the Wikipedia article, in `extract_language`; None when there is no article.
    extract: str | None
    extract_language: CardLanguage | None
    sources: tuple[Source, ...]

    def as_prompt(self) -> dict[str, Any]:
        """Only public facts; nothing about the person who asked."""
        return {
            "scientific_name": self.scientific_name, "order": self.order, "family": self.family, "genus": self.genus,
            "iucn_red_list_category": self.iucn_category, "wikipedia_extract": self.extract, "wikipedia_language": self.extract_language,
        }


def gather(scientific_name: str, language: CardLanguage) -> SpeciesFacts:
    """Raises SPECIES_UNKNOWN for a name that is not an exact bird species, PUBLIC_DATA_UNAVAILABLE when GBIF fails."""
    timeout = get_settings().public_data_timeout_seconds
    with httpx.Client(timeout=timeout, headers={"User-Agent": USER_AGENT}) as client:
        try:
            match = client.get(f"{GBIF_API_URL}/species/match", params={"name": scientific_name, "kingdom": "Animalia", "class": "Aves"})
            match.raise_for_status()
            taxon = parse_taxon(match.json())
        except (httpx.HTTPError, ValueError):
            raise_error(ErrorCode.PUBLIC_DATA_UNAVAILABLE)
        if taxon is None:
            raise_error(ErrorCode.SPECIES_UNKNOWN)
        key, order, family, genus = taxon
        category = _iucn(client, key)
        extract, extract_language, article = _summary(client, scientific_name, language)
    sources = [Source("GBIF", GBIF_SPECIES_PAGE.format(key=key), GBIF_LICENSE)]
    if article:
        sources.append(Source("Wikipedia", article, WIKIPEDIA_LICENSE))
    return SpeciesFacts(scientific_name, order, family, genus, category, extract, extract_language, tuple(sources))


def parse_taxon(body: object) -> tuple[int, str, str, str] | None:
    """Key, order, family and genus of an exact bird-species match; the same rule as the app's gbif.ts."""
    if not isinstance(body, dict):
        return None
    if body.get("matchType") != "EXACT" or body.get("rank") != "SPECIES" or body.get("kingdom") != "Animalia" or body.get("class") != "Aves":
        return None
    # BirdNET's names follow another checklist, so some are synonyms in GBIF: the accepted species carries the facts.
    key = body.get("acceptedUsageKey", body.get("usageKey"))
    order, family, genus = body.get("order"), body.get("family"), body.get("genus")
    if not isinstance(key, int) or isinstance(key, bool) or key <= 0:
        return None
    if not (isinstance(order, str) and order.strip() and isinstance(family, str) and family.strip() and isinstance(genus, str) and genus.strip()):
        return None
    return key, order.strip(), family.strip(), genus.strip()


def parse_summary(body: object) -> tuple[str, str] | None:
    """Extract and article URL of a Wikipedia summary, or None for a disambiguation page or a malformed answer."""
    if not isinstance(body, dict) or body.get("type") == DISAMBIGUATION:
        return None
    extract = body.get("extract")
    page = body.get("content_urls", {}).get("desktop", {}).get("page") if isinstance(body.get("content_urls"), dict) else None
    if not isinstance(extract, str) or not extract.strip() or not isinstance(page, str) or not page.startswith("https://"):
        return None
    return extract.strip()[:MAX_EXTRACT_LENGTH], page


def _iucn(client: httpx.Client, key: int) -> str | None:
    # The category is never guessed: without an answer the card simply does not name one.
    try:
        response = client.get(f"{GBIF_API_URL}/species/{key}/iucnRedListCategory")
        if response.status_code == NO_CONTENT or response.is_error:
            return None
        code = response.json().get("code")
    except (httpx.HTTPError, ValueError, AttributeError):
        return None
    return code if code in IUCN_CODES else None


def _summary(client: httpx.Client, scientific_name: str, language: CardLanguage) -> tuple[str | None, CardLanguage | None, str | None]:
    title = quote(scientific_name.replace(" ", "_"), safe="")
    for candidate in dict.fromkeys((language, FALLBACK_LANGUAGE)):
        try:
            response = client.get(WIKIPEDIA_SUMMARY_URL.format(language=candidate.value, title=title), follow_redirects=True)
            if response.status_code == NOT_FOUND:
                continue
            response.raise_for_status()
            parsed = parse_summary(response.json())
        except (httpx.HTTPError, ValueError, AttributeError) as error:
            # GBIF alone still makes a short card; Wikipedia's absence is said by the sources listed.
            logger.warning("Wikipedia summary unavailable: %s", type(error).__name__)
            return None, None, None
        if parsed:
            return parsed[0], candidate, parsed[1]
    return None, None, None
