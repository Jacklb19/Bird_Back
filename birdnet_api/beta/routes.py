"""Private beta resource routes, kept apart from the established API endpoints."""
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query

from ..auth import authenticate
from ..domain import MAX_SPECIES_LENGTH, SCIENTIFIC_NAME_PATTERN
from .contracts import (
    Collection, CollectionInput, CollectionItemPage, CollectionPage, DeleteQuery, DeletionPage, DeletionQuery,
    DescendingQuery, EventKind, EventQuery, Observation, ObservationInput, ObservationPage, ObservationQuery,
    Outing, OutingInput, OutingPage, TimelinePage, WishlistItem, WishlistPage, WishlistQuery,
)
from .repository import JournalRepository, OrganizationRepository

router = APIRouter(prefix="/v1")
ScientificName = Annotated[str, Path(min_length=1, max_length=MAX_SPECIES_LENGTH, pattern=f"^{SCIENTIFIC_NAME_PATTERN}$")]


def journal() -> JournalRepository:
    return JournalRepository()


def organization() -> OrganizationRepository:
    return OrganizationRepository()


@router.get("/outings", response_model=OutingPage)
def list_outings(query: Annotated[DescendingQuery, Query()], owner: UUID = Depends(authenticate), repo: JournalRepository = Depends(journal)) -> OutingPage:
    return repo.outings(owner, query)


@router.put("/outings/{identifier}", response_model=Outing)
def save_outing(identifier: UUID, payload: OutingInput, owner: UUID = Depends(authenticate), repo: JournalRepository = Depends(journal)) -> Outing:
    return repo.save_outing(owner, identifier, payload)


@router.get("/outings/{identifier}", response_model=Outing)
def get_outing(identifier: UUID, owner: UUID = Depends(authenticate), repo: JournalRepository = Depends(journal)) -> Outing:
    return repo.outing(owner, identifier)


@router.delete("/outings/{identifier}", status_code=204)
def delete_outing(identifier: UUID, query: Annotated[DeleteQuery, Query()], owner: UUID = Depends(authenticate), repo: JournalRepository = Depends(journal)) -> None:
    repo.remove(owner, "outing", identifier, query.expected_version)


@router.get("/outings/{identifier}/timeline", response_model=TimelinePage)
def outing_timeline(identifier: UUID, query: Annotated[EventQuery, Query()], owner: UUID = Depends(authenticate), repo: JournalRepository = Depends(journal)) -> TimelinePage:
    return repo.timeline(owner, identifier, query)


@router.put("/outings/{identifier}/detections/{detection_id}", status_code=204)
def attach_detection(identifier: UUID, detection_id: UUID, owner: UUID = Depends(authenticate), repo: JournalRepository = Depends(journal)) -> None:
    repo.link_detection(owner, identifier, detection_id)


@router.delete("/outings/{identifier}/detections/{detection_id}", status_code=204)
def detach_detection(identifier: UUID, detection_id: UUID, owner: UUID = Depends(authenticate), repo: JournalRepository = Depends(journal)) -> None:
    repo.unlink_detection(owner, identifier, detection_id)


@router.get("/observations", response_model=ObservationPage)
def list_observations(query: Annotated[ObservationQuery, Query()], owner: UUID = Depends(authenticate), repo: JournalRepository = Depends(journal)) -> ObservationPage:
    return repo.observations(owner, query)


@router.put("/observations/{identifier}", response_model=Observation)
def save_observation(identifier: UUID, payload: ObservationInput, owner: UUID = Depends(authenticate), repo: JournalRepository = Depends(journal)) -> Observation:
    return repo.save_observation(owner, identifier, payload)


@router.get("/observations/{identifier}", response_model=Observation)
def get_observation(identifier: UUID, owner: UUID = Depends(authenticate), repo: JournalRepository = Depends(journal)) -> Observation:
    return repo.observation(owner, identifier)


@router.delete("/observations/{identifier}", status_code=204)
def delete_observation(identifier: UUID, query: Annotated[DeleteQuery, Query()], owner: UUID = Depends(authenticate), repo: JournalRepository = Depends(journal)) -> None:
    repo.remove(owner, "observation", identifier, query.expected_version)


@router.get("/collections", response_model=CollectionPage)
def list_collections(query: Annotated[DescendingQuery, Query()], owner: UUID = Depends(authenticate), repo: OrganizationRepository = Depends(organization)) -> CollectionPage:
    return repo.collections(owner, query)


@router.put("/collections/{identifier}", response_model=Collection)
def save_collection(identifier: UUID, payload: CollectionInput, owner: UUID = Depends(authenticate), repo: OrganizationRepository = Depends(organization)) -> Collection:
    return repo.save_collection(owner, identifier, payload)


@router.get("/collections/{identifier}", response_model=Collection)
def get_collection(identifier: UUID, owner: UUID = Depends(authenticate), repo: OrganizationRepository = Depends(organization)) -> Collection:
    return repo.collection(owner, identifier)


@router.delete("/collections/{identifier}", status_code=204)
def delete_collection(identifier: UUID, query: Annotated[DeleteQuery, Query()], owner: UUID = Depends(authenticate), repo: OrganizationRepository = Depends(organization)) -> None:
    repo.remove(owner, "collection", identifier, query.expected_version)


@router.put("/collections/{identifier}/items/{kind}/{record_id}", status_code=204)
def attach_item(identifier: UUID, kind: EventKind, record_id: UUID, owner: UUID = Depends(authenticate), repo: OrganizationRepository = Depends(organization)) -> None:
    repo.link_item(owner, identifier, kind, record_id)


@router.delete("/collections/{identifier}/items/{kind}/{record_id}", status_code=204)
def detach_item(identifier: UUID, kind: EventKind, record_id: UUID, owner: UUID = Depends(authenticate), repo: OrganizationRepository = Depends(organization)) -> None:
    repo.unlink_item(owner, identifier, kind, record_id)


@router.get("/collections/{identifier}/items", response_model=CollectionItemPage)
def collection_items(identifier: UUID, query: Annotated[EventQuery, Query()], owner: UUID = Depends(authenticate), repo: OrganizationRepository = Depends(organization)) -> CollectionItemPage:
    return repo.items(owner, identifier, query)


@router.put("/wishlist/{species}", response_model=WishlistItem)
def add_wish(species: ScientificName, owner: UUID = Depends(authenticate), repo: OrganizationRepository = Depends(organization)) -> WishlistItem:
    return repo.wish(owner, species)


@router.delete("/wishlist/{species}", status_code=204)
def delete_wish(species: ScientificName, owner: UUID = Depends(authenticate), repo: OrganizationRepository = Depends(organization)) -> None:
    repo.unwish(owner, species)


@router.get("/wishlist", response_model=WishlistPage)
def list_wishes(query: Annotated[WishlistQuery, Query()], owner: UUID = Depends(authenticate), repo: OrganizationRepository = Depends(organization)) -> WishlistPage:
    return repo.wishlist(owner, query)


@router.get("/deletions", response_model=DeletionPage)
def list_deletions(query: Annotated[DeletionQuery, Query()], owner: UUID = Depends(authenticate), repo: JournalRepository = Depends(journal)) -> DeletionPage:
    return repo.deletions(owner, query)
