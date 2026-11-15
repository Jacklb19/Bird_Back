"""A closed set of deterministic, read-only queries; private rows never reach a text provider."""
from typing import Any
from uuid import UUID

from ..domain import DB_TO_API_STATUS, DbDetectionStatus
from ..model_species import require_model_species
from ..repository import require_row
from .contracts import (
    AssistantAnswer, AssistantQuestion, CollectionTotals, CollectionsAnswer, CollectionsQuestion,
    DetectionFilters, DetectionTotals, DetectionsAnswer, DetectionsQuestion, EmptyFilters,
    ObservationFilters, ObservationTotals, ObservationsAnswer, ObservationsQuestion, OutingTotals,
    OutingsAnswer, OutingsQuestion, PeriodFilters, QueryCollection, QueryDetection, QueryObservation,
    QueryOuting, QuerySpecies, SpeciesAnswer, SpeciesFilters, SpeciesQuestion, WishlistAnswer,
    WishlistFilters, WishlistItem, WishlistQuestion, WishlistTotals,
)
from .repository import OrganizationRepository, require_owned

ACOUSTIC_FILTERS = """user_id=%(owner)s AND (%(species)s::text IS NULL OR especie=%(species)s)
    AND (%(since)s::timestamptz IS NULL OR momento >= %(since)s)
    AND (%(until)s::timestamptz IS NULL OR momento < %(until)s)"""
VISUAL_FILTERS = """owner_id=%(owner)s AND (%(species)s::text IS NULL OR species=%(species)s)
    AND (%(since)s::timestamptz IS NULL OR observed_at >= %(since)s)
    AND (%(until)s::timestamptz IS NULL OR observed_at < %(until)s)
    AND (%(outing_id)s::uuid IS NULL OR outing_id=%(outing_id)s)"""
OUTING_FILTERS = """owner_id=%(owner)s AND (%(since)s::timestamptz IS NULL OR started_at >= %(since)s)
    AND (%(until)s::timestamptz IS NULL OR started_at < %(until)s)"""
DESCENDING_CURSOR = "(%(before)s::timestamptz IS NULL OR {time} < %(before)s OR ({time}=%(before)s AND id>%(after_id)s::uuid))"


class QueryRepository(OrganizationRepository):
    def query(self, owner: UUID, question: AssistantQuestion) -> AssistantAnswer:
        species = getattr(question, "species", None)
        if species is not None:
            require_model_species(species)
        # The discriminated input union is exhaustive; neither a table nor an expression comes from the caller.
        if isinstance(question, DetectionsQuestion):
            return self.detections_answer(owner, question)
        if isinstance(question, SpeciesQuestion):
            return self.species_answer(owner, question)
        if isinstance(question, OutingsQuestion):
            return self.outings_answer(owner, question)
        if isinstance(question, ObservationsQuestion):
            return self.observations_answer(owner, question)
        if isinstance(question, CollectionsQuestion):
            return self.collections_answer(owner, question)
        return self.wishlist_answer(owner, question)

    @staticmethod
    def parameters(owner: UUID, question: AssistantQuestion) -> dict[str, Any]:
        return {**question.model_dump(), "owner": owner, "fetch": question.limit + 1}

    def detections_answer(self, owner: UUID, question: DetectionsQuestion) -> DetectionsAnswer:
        parameters = self.parameters(owner, question)
        where = ACOUSTIC_FILTERS + " AND (%(review)s='all' OR estado='provisional')"
        with self.transaction(owner) as c:
            totals = require_row(c.execute(f"SELECT count(*) AS detections,count(DISTINCT especie) AS species FROM public.detections WHERE {where}", parameters).fetchone())
            rows = c.execute(f"""SELECT id,especie AS species,confianza AS confidence,estado AS status,momento AS recorded_at,
                ruta_grabacion IS NOT NULL AS has_recording FROM public.detections WHERE {where}
                AND {DESCENDING_CURSOR.format(time='momento')} ORDER BY momento DESC,id LIMIT %(fetch)s""", parameters).fetchall()
        for row in rows:
            row["status"] = DB_TO_API_STATUS[DbDetectionStatus(row["status"])]
        return DetectionsAnswer(filters=DetectionFilters(species=question.species, since=question.since, until=question.until, review=question.review),
            totals=DetectionTotals.model_validate(totals), detections=[QueryDetection.model_validate(row) for row in rows[:question.limit]], has_more=len(rows) > question.limit)

    def species_answer(self, owner: UUID, question: SpeciesQuestion) -> SpeciesAnswer:
        parameters = self.parameters(owner, question)
        where = ACOUSTIC_FILTERS + " AND estado <> 'descartada'"
        with self.transaction(owner) as c:
            totals = require_row(c.execute(f"SELECT count(*) AS detections,count(DISTINCT especie) AS species FROM public.detections WHERE {where}", parameters).fetchone())
            rows = c.execute(f"""SELECT especie AS species,count(*) AS detections,max(confianza) AS best_confidence,
                min(momento) AS first_recorded_at,max(momento) AS last_recorded_at FROM public.detections WHERE {where}
                AND (%(after_species)s::text IS NULL OR especie>%(after_species)s) GROUP BY especie ORDER BY especie LIMIT %(fetch)s""", parameters).fetchall()
        return SpeciesAnswer(filters=SpeciesFilters(species=question.species, since=question.since, until=question.until),
            totals=DetectionTotals.model_validate(totals), species=[QuerySpecies.model_validate(row) for row in rows[:question.limit]], has_more=len(rows) > question.limit)

    def outings_answer(self, owner: UUID, question: OutingsQuestion) -> OutingsAnswer:
        parameters = self.parameters(owner, question)
        with self.transaction(owner) as c:
            total = require_row(c.execute(f"SELECT count(*) AS outings FROM public.outings WHERE {OUTING_FILTERS}", parameters).fetchone())
            rows = c.execute(f"SELECT id FROM public.outings WHERE {OUTING_FILTERS} AND {DESCENDING_CURSOR.format(time='started_at')} ORDER BY started_at DESC,id LIMIT %(fetch)s", parameters).fetchall()
            result = [QueryOuting.model_validate(self._outing(c, owner, row["id"]).model_dump(include=set(QueryOuting.model_fields))) for row in rows[:question.limit]]
        return OutingsAnswer(filters=PeriodFilters(since=question.since, until=question.until), totals=OutingTotals.model_validate(total), outings=result, has_more=len(rows) > question.limit)

    def observations_answer(self, owner: UUID, question: ObservationsQuestion) -> ObservationsAnswer:
        parameters = self.parameters(owner, question)
        with self.transaction(owner) as c:
            if question.outing_id is not None:
                require_owned(c, "outings", owner, question.outing_id)
            totals = require_row(c.execute(f"SELECT count(*) AS observations,count(DISTINCT species) AS species FROM public.observations WHERE {VISUAL_FILTERS}", parameters).fetchone())
            rows = c.execute(f"""SELECT id,version,species,observed_at,individuals,outing_id FROM public.observations WHERE {VISUAL_FILTERS}
                AND {DESCENDING_CURSOR.format(time='observed_at')} ORDER BY observed_at DESC,id LIMIT %(fetch)s""", parameters).fetchall()
        return ObservationsAnswer(filters=ObservationFilters(species=question.species, since=question.since, until=question.until, outing_id=question.outing_id),
            totals=ObservationTotals.model_validate(totals), observations=[QueryObservation.model_validate(row) for row in rows[:question.limit]], has_more=len(rows) > question.limit)

    def collections_answer(self, owner: UUID, question: CollectionsQuestion) -> CollectionsAnswer:
        parameters = self.parameters(owner, question)
        with self.transaction(owner) as c:
            total = require_row(c.execute("SELECT count(*) AS collections FROM public.collections WHERE owner_id=%(owner)s", parameters).fetchone())
            rows = c.execute(f"SELECT id FROM public.collections WHERE owner_id=%(owner)s AND {DESCENDING_CURSOR.format(time='created_at')} ORDER BY created_at DESC,id LIMIT %(fetch)s", parameters).fetchall()
            result = [QueryCollection.model_validate(self._collection(c, owner, row["id"]).model_dump(include=set(QueryCollection.model_fields))) for row in rows[:question.limit]]
        return CollectionsAnswer(filters=EmptyFilters(), totals=CollectionTotals.model_validate(total), collections=result, has_more=len(rows) > question.limit)

    def wishlist_answer(self, owner: UUID, question: WishlistQuestion) -> WishlistAnswer:
        parameters = self.parameters(owner, question)
        where = "owner_id=%(owner)s AND (%(species)s::text IS NULL OR species=%(species)s)"
        with self.transaction(owner) as c:
            total = require_row(c.execute(f"SELECT count(*) AS species FROM public.wishlist WHERE {where}", parameters).fetchone())
            rows = c.execute(f"SELECT species,created_at FROM public.wishlist WHERE {where} AND (%(after_species)s::text IS NULL OR species>%(after_species)s) ORDER BY species LIMIT %(fetch)s", parameters).fetchall()
        return WishlistAnswer(filters=WishlistFilters(species=question.species), totals=WishlistTotals.model_validate(total),
            wishlist=[WishlistItem.model_validate(row) for row in rows[:question.limit]], has_more=len(rows) > question.limit)
