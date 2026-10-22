"""Site reports written only when the person asks (RF-15, ADR-37), from the deterministic statistics of the site.

The model does not infer anything: the API computes every figure (including differences and the busiest hour) and
the model only puts them into words. A text that writes a number the figures do not contain is refused. The prompt
names neither the site nor the person nor any coordinate.
"""
import hashlib
import json
import re
from datetime import datetime
from typing import Any, Final
from uuid import UUID
from zoneinfo import ZoneInfo

from .contracts import SiteReport, SiteStats
from .domain import MAX_REPORT_SPECIES, SITE_REPORT_INSTRUCTIONS_VERSION, CardLanguage, StatsPeriod
from .errors import ErrorCode, raise_error
from .language_model import TextWriter
from .llm_usage import UsageRepository
from .repository import DetectionRepository
from .settings import get_settings

LANGUAGE_NAMES: Final = {CardLanguage.ES: "Spanish (as written in Colombia)", CardLanguage.EN: "English"}
INSTRUCTIONS: Final = """You write a short report of what a bird-sound app recorded at one place, for the person who
monitors it, in {language}. Use only the figures in the JSON you receive and write every number with digits, exactly
as given. Never compute a number yourself (no percentages, averages, sums or differences): when a difference matters it
is given. Never say or suggest that a species is absent, gone, lost, declining or missing from the place: a species
"not detected in this period" only was not heard by the app, and it may still be there. Do not explain causes or
ecological trends, do not give advice, and do not name the place. Detections are automatic and can be wrong; do not
present them as certain. Write 2 to 4 short paragraphs of plain prose, without lists, headings or markdown.
Answer as JSON: {{"paragraphs": ["..."]}}."""
# Attempts at a text whose numbers all come from the figures; a second miss is reported as unavailable.
WRITE_ATTEMPTS: Final = 2
# Integers with optional thousands separators ("1.234", "1,234"), as either language writes them.
NUMBER: Final = re.compile(r"\d{1,3}(?:[.,]\d{3})+(?!\d)|\d+")
HOUR_RANGE_TEMPLATE: Final = "{start:02d}:00-{end:02d}:00"


def fingerprint(stats: SiteStats, tz: str) -> str:
    """Digest of the figures, without the instants of the period, which move with every request."""
    figures = stats.model_dump(mode="json", exclude={"since", "until"})
    return hashlib.sha256(json.dumps([figures, tz], sort_keys=True).encode()).hexdigest()


def report_facts(stats: SiteStats, tz: str, names: dict[str, str]) -> dict[str, Any]:
    """Everything the text may say, already computed. Species go by the name the person's app shows them with."""
    zone = ZoneInfo(tz)
    named = lambda species: names.get(species, species)  # noqa: E731
    busiest = max(range(len(stats.hourly)), key=lambda hour: stats.hourly[hour]) if stats.detections else None
    facts: dict[str, Any] = {
        "period": "the whole record" if stats.period is StatsPeriod.ALL else f"the last {stats.period.days} days",
        "from_date": stats.since.astimezone(zone).date().isoformat() if stats.since else None,
        "to_date": stats.until.astimezone(zone).date().isoformat(),
        "species_detected": stats.species_count,
        "detections": stats.detections,
        "days_with_detections": stats.active_days,
        "busiest_hour": HOUR_RANGE_TEMPLATE.format(start=busiest, end=(busiest + 1) % 24) if busiest is not None else None,
        "detections_in_busiest_hour": stats.hourly[busiest] if busiest is not None else None,
        "species": [{"name": named(s.species), "scientific_name": s.species, "detections": s.detections, "days": s.days,
                     "first_time_at_this_place": s.is_new} for s in stats.species[:MAX_REPORT_SPECIES]],
        "species_first_detected_in_this_period": [named(s.species) for s in stats.species if s.is_new],
        "species_detected_before_but_not_in_this_period": [named(species) for species in stats.missing],
    }
    if stats.previous_species_count is not None:
        facts["species_detected_in_the_previous_period_of_the_same_length"] = stats.previous_species_count
        facts["difference_in_species_detected"] = abs(stats.species_count - stats.previous_species_count)
        facts["more_or_fewer_species_than_previous_period"] = (
            "more" if stats.species_count > stats.previous_species_count else "fewer" if stats.species_count < stats.previous_species_count else "same")
    return facts


def allowed_numbers(value: object) -> set[int]:
    """Every integer in the figures, including those inside dates and hours."""
    found: set[int] = set()
    if isinstance(value, bool) or value is None:
        return found
    if isinstance(value, int):
        found.add(abs(value))
    elif isinstance(value, str):
        found.update(int(digits) for digits in re.findall(r"\d+", value))
    elif isinstance(value, dict):
        for item in value.values():
            found |= allowed_numbers(item)
    elif isinstance(value, list):
        for item in value:
            found |= allowed_numbers(item)
    return found


def numbers_in(text: str) -> set[int]:
    return {int(re.sub(r"[.,]", "", match)) for match in NUMBER.findall(text)}


def only_given_numbers(paragraphs: list[str], facts: dict[str, Any]) -> bool:
    allowed = allowed_numbers(facts)
    return all(numbers_in(paragraph) <= allowed for paragraph in paragraphs)


class SiteReportRepository(DetectionRepository):
    def get(self, owner: UUID, site_id: UUID, period: StatsPeriod, language: CardLanguage) -> dict[str, Any] | None:
        with self.transaction(owner) as connection:
            return connection.execute(
                """SELECT periodo, idioma, zona_horaria, cifras, huella, parrafos, modelo, version_instrucciones, generated_at
                FROM public.site_reports WHERE site_id = %s AND periodo = %s AND idioma = %s""",
                (site_id, period.value, language.value)).fetchone()

    def save(self, owner: UUID, site_id: UUID, stats: SiteStats, tz: str, language: CardLanguage, paragraphs: list[str], model: str) -> dict[str, Any]:
        # The caller has just read the site's statistics under RLS, which proves the site is the owner's.
        with self.trusted_transaction() as connection:
            return connection.execute(
                """INSERT INTO public.site_reports (site_id, user_id, periodo, idioma, zona_horaria, cifras, huella, parrafos, modelo, version_instrucciones)
                VALUES (%(site)s, %(owner)s, %(period)s, %(language)s, %(tz)s, %(stats)s, %(digest)s, %(paragraphs)s, %(model)s, %(version)s)
                ON CONFLICT (site_id, periodo, idioma) DO UPDATE SET zona_horaria = excluded.zona_horaria, cifras = excluded.cifras,
                  huella = excluded.huella, parrafos = excluded.parrafos, modelo = excluded.modelo,
                  version_instrucciones = excluded.version_instrucciones, generated_at = now()
                RETURNING periodo, idioma, zona_horaria, cifras, huella, parrafos, modelo, version_instrucciones, generated_at""",
                {"site": site_id, "owner": owner, "period": stats.period.value, "language": language.value, "tz": tz,
                 "stats": stats.model_dump_json(), "digest": fingerprint(stats, tz), "paragraphs": json.dumps(paragraphs, ensure_ascii=False),
                 "model": model, "version": SITE_REPORT_INSTRUCTIONS_VERSION}).fetchone()


def is_current(row: dict[str, Any], stats: SiteStats, tz: str, model: str) -> bool:
    return row["huella"] == fingerprint(stats, tz) and row["modelo"] == model and row["version_instrucciones"] == SITE_REPORT_INSTRUCTIONS_VERSION


def to_report(row: dict[str, Any], stale: bool) -> SiteReport:
    return SiteReport(period=StatsPeriod(row["periodo"]), locale=CardLanguage(row["idioma"]), paragraphs=list(row["parrafos"]),
                      stats=SiteStats.model_validate(row["cifras"]), model=row["modelo"], generated_at=row["generated_at"], stale=stale)


def report(owner: UUID, site_id: UUID, stats: SiteStats, tz: str, language: CardLanguage, names: dict[str, str], current: datetime,
           reports: SiteReportRepository, usage: UsageRepository, writer: TextWriter) -> SiteReport:
    """The stored report while its figures are the same; otherwise a new one, counted against the caller's texts."""
    if stats.detections == 0:
        # Nothing was heard: there is nothing to put into words, and no text may turn that into an absence.
        raise_error(ErrorCode.NOTHING_TO_REPORT)
    stored = reports.get(owner, site_id, stats.period, language)
    if stored is not None and is_current(stored, stats, tz, writer.model):
        return to_report(stored, stale=False)
    settings = get_settings()
    usage.spend(owner, current, settings.llm_user_daily_quota, settings.llm_daily_quota)
    facts = report_facts(stats, tz, names)
    try:
        for _ in range(WRITE_ATTEMPTS):
            paragraphs = writer.write(INSTRUCTIONS.format(language=LANGUAGE_NAMES[language]), facts)
            if only_given_numbers(paragraphs, facts):
                return to_report(reports.save(owner, site_id, stats, tz, language, paragraphs, writer.model), stale=False)
    except Exception:
        usage.give_back(owner, current)
        raise
    usage.give_back(owner, current)
    raise_error(ErrorCode.LLM_UNAVAILABLE)
