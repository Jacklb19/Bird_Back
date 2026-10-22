"""Species cards written by the language model from public facts (RF-14, ADR-37), shared by everyone.

One card per species and language: the first person who asks for it spends one text of their day, and everyone
else reads the stored card. A card is written again only once it is stale (old, or written by another model or
other instructions), never because someone asks twice.
"""
import json
from datetime import datetime, timedelta
from typing import Any, Final
from uuid import UUID

from fastapi import HTTPException

from .contracts import CardSource, SpeciesCard
from .domain import SPECIES_CARD_INSTRUCTIONS_VERSION, CardLanguage
from .errors import ErrorCode
from .language_model import TextWriter
from .llm_usage import UsageRepository
from .public_sources import SpeciesFacts, gather
from .repository import DetectionRepository
from .settings import get_settings

LANGUAGE_NAMES: Final = {CardLanguage.ES: "Spanish (as written in Colombia)", CardLanguage.EN: "English"}
IUCN_NAMES: Final = {
    "LC": "Least Concern", "NT": "Near Threatened", "VU": "Vulnerable", "EN": "Endangered", "CR": "Critically Endangered",
    "EW": "Extinct in the Wild", "EX": "Extinct", "DD": "Data Deficient", "NE": "Not Evaluated",
}
INSTRUCTIONS: Final = """You write a short field-guide card about one bird species for a general audience, in {language}.
Use only the facts in the JSON you receive. Never add a fact that is not stated there: no sizes, colours, songs,
behaviour, diet, places or numbers unless the facts state them. If there is no Wikipedia extract, write a single short
paragraph from the classification and the IUCN Red List category alone. Never say whether the bird lives or was heard
at any particular place. Name the species by its common name when the extract gives one, followed by its scientific
name in the first sentence. Write 1 to 3 short paragraphs of plain prose, without lists, headings or markdown.
Answer as JSON: {{"paragraphs": ["..."]}}."""


class SpeciesCardRepository(DetectionRepository):
    def get(self, species: str, language: CardLanguage) -> dict[str, Any] | None:
        with self.trusted_transaction() as connection:
            return connection.execute(
                """SELECT especie, idioma, parrafos, fuentes, modelo, version_instrucciones, generated_at
                FROM public.species_cards WHERE especie = %s AND idioma = %s""", (species, language.value)).fetchone()

    def save(self, species: str, language: CardLanguage, paragraphs: list[str], sources: list[CardSource], model: str) -> dict[str, Any]:
        with self.trusted_transaction() as connection:
            return connection.execute(
                """INSERT INTO public.species_cards (especie, idioma, parrafos, fuentes, modelo, version_instrucciones)
                VALUES (%(species)s, %(language)s, %(paragraphs)s, %(sources)s, %(model)s, %(version)s)
                ON CONFLICT (especie, idioma) DO UPDATE SET parrafos = excluded.parrafos, fuentes = excluded.fuentes,
                  modelo = excluded.modelo, version_instrucciones = excluded.version_instrucciones, generated_at = now()
                RETURNING especie, idioma, parrafos, fuentes, modelo, version_instrucciones, generated_at""",
                {"species": species, "language": language.value, "paragraphs": json.dumps(paragraphs, ensure_ascii=False),
                 "sources": json.dumps([s.model_dump() for s in sources], ensure_ascii=False), "model": model,
                 "version": SPECIES_CARD_INSTRUCTIONS_VERSION}).fetchone()


def is_stale(row: dict[str, Any], current: datetime, model: str) -> bool:
    max_age = timedelta(days=get_settings().species_card_max_age_days)
    return row["modelo"] != model or row["version_instrucciones"] != SPECIES_CARD_INSTRUCTIONS_VERSION or row["generated_at"] < current - max_age


def to_card(row: dict[str, Any], current: datetime, model: str) -> SpeciesCard:
    return SpeciesCard(
        species=row["especie"], locale=CardLanguage(row["idioma"]), paragraphs=list(row["parrafos"]),
        sources=[CardSource.model_validate(source) for source in row["fuentes"]], model=row["modelo"],
        generated_at=row["generated_at"], stale=is_stale(row, current, model),
    )


def facts_for_prompt(facts: SpeciesFacts) -> dict[str, Any]:
    prompt = facts.as_prompt()
    if facts.iucn_category:
        prompt["iucn_red_list_category"] = f"{facts.iucn_category} ({IUCN_NAMES[facts.iucn_category]})"
    return prompt


def card(owner: UUID, species: str, language: CardLanguage, current: datetime, cards: SpeciesCardRepository,
         usage: UsageRepository, writer: TextWriter) -> SpeciesCard:
    """The stored card while it is fresh; otherwise a new one, counted against the caller's texts of the day."""
    stored = cards.get(species, language)
    if stored is not None and not is_stale(stored, current, writer.model):
        return to_card(stored, current, writer.model)
    settings = get_settings()
    usage.spend(owner, current, settings.llm_user_daily_quota, settings.llm_daily_quota)
    try:
        facts = gather(species, language)
        paragraphs = writer.write(INSTRUCTIONS.format(language=LANGUAGE_NAMES[language]), facts_for_prompt(facts))
    except Exception as error:
        # A name GBIF does not know is the caller's mistake and still counts; a source or model that failed does not.
        if not (isinstance(error, HTTPException) and error.detail == ErrorCode.SPECIES_UNKNOWN.message):
            usage.give_back(owner, current)
        raise
    sources = [CardSource(name=s.name, url=s.url, license=s.license) for s in facts.sources]
    return to_card(cards.save(species, language, paragraphs, sources, writer.model), current, writer.model)
