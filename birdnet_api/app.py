"""Synchronization, map, review, saved recordings, profile, personal record, monitoring sites, statistics, CSV export and the model manifest."""
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from typing import Annotated, Any, Final
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, Path, Query, Request
from fastapi.responses import JSONResponse, Response

from .auth import authenticate
from .contracts import (
    AvatarInput,
    AvatarUpload,
    BatchInput,
    BatchResponse,
    ExportQuery,
    MapQuery,
    MapResponse,
    OwnSpeciesList,
    Profile,
    ProfileInput,
    RecordingInput,
    RecordingLink,
    RecordingsDeleted,
    RecordingUpload,
    RecordingUrl,
    RecordingUsage,
    RecordSummary,
    ReviewedDetection,
    ReviewInput,
    SharingInput,
    SharingUpdate,
    Site,
    SiteInput,
    SiteList,
    SiteStats,
    SpeciesRecord,
    StatsQuery,
    TimeZoneQuery,
)
from .domain import MAX_SPECIES_LENGTH, local_status
from .errors import ErrorCode, error_response, raise_error
from .manifest import load_manifest
from .profiles import ProfileRepository
from .recordings import RecordingRepository
from .records import RecordRepository
from .repository import DetectionRepository
from .settings import SettingsError, get_settings
from .sites import SiteRepository
from .storage import AvatarStorage, RecordingStorage

# Version prefix shared by every public route.
API_PREFIX: Final = "/v1"
# Tells the client that the export stopped at EXPORT_ROW_LIMIT rows.
TRUNCATED_HEADER: Final = "X-Truncated"
EXPORT_FILENAME_TEMPLATE: Final = "birdnet-{site_id}.csv"
EXPORT_MEDIA_TYPE: Final = "text/csv; charset=utf-8"
# Methods whose bodies are read into memory and therefore bounded by MAX_METADATA_BODY_BYTES.
BOUNDED_BODY_METHODS: Final = frozenset({"POST", "PATCH", "PUT"})

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # Report malformed configuration at startup rather than on the first request that reads it.
    get_settings()
    yield


# Deployed as its own project: routes live at the root and the web app proxies /api/* here.
app = FastAPI(title="Trino API", docs_url=None, redoc_url=None, lifespan=lifespan)
router = APIRouter(prefix=API_PREFIX)


def repository() -> DetectionRepository:
    return DetectionRepository()


def sites() -> SiteRepository:
    return SiteRepository()


def now() -> datetime:
    return datetime.now(UTC)


def profiles() -> ProfileRepository:
    return ProfileRepository()


def avatars() -> AvatarStorage:
    return AvatarStorage()


def records() -> RecordRepository:
    return RecordRepository()


def recordings() -> RecordingRepository:
    return RecordingRepository()


def clips() -> RecordingStorage:
    return RecordingStorage()


@app.exception_handler(SettingsError)
async def invalid_configuration(_: Request, error: SettingsError) -> JSONResponse:
    # The message names the variable; it goes to the operator's logs, not to the client.
    logger.error("Invalid configuration: %s", error)
    return error_response(ErrorCode.CONFIGURATION_INVALID)


@app.middleware("http")
async def bounded_body(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    # The API never accepts audio, images or unbounded JSON bodies: files go straight to Storage. Only its own
    # routes are bounded; whatever a test server mounts beside them is not part of the API.
    if request.method in BOUNDED_BODY_METHODS and request.url.path.startswith(API_PREFIX):
        try:
            limit = get_settings().max_metadata_body_bytes
        except SettingsError as error:
            # Middleware runs outside the exception handlers, so it reports the error itself.
            return await invalid_configuration(request, error)
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > limit:
                return error_response(ErrorCode.BODY_TOO_LARGE)
        request._body = bytes(data)
    return await call_next(request)


@router.post("/detections/batch", response_model=BatchResponse)
def batch(payload: BatchInput, owner: UUID = Depends(authenticate), repo: DetectionRepository = Depends(repository)) -> BatchResponse:
    for row in payload.detections:
        if row.status != local_status(row.confidence):
            raise_error(ErrorCode.STATUS_CONFIDENCE_MISMATCH)
    return repo.batch(owner, payload.detections)


@router.patch("/detections/{detection_id}", response_model=ReviewedDetection)
def review(detection_id: UUID, payload: ReviewInput, owner: UUID = Depends(authenticate), repo: DetectionRepository = Depends(repository)) -> ReviewedDetection:
    reviewed = repo.review(owner, detection_id, payload.action, payload.species)
    if reviewed is None:
        raise_error(ErrorCode.DETECTION_NOT_FOUND)
    return reviewed


@router.post("/detections/{detection_id}/recording-url", response_model=RecordingUpload)
def recording_upload(detection_id: UUID, payload: RecordingInput, owner: UUID = Depends(authenticate), repo: DetectionRepository = Depends(repository),
                     saved: RecordingRepository = Depends(recordings), storage: RecordingStorage = Depends(clips)) -> RecordingUpload:
    if repo.get_owned(owner, detection_id) is None:
        raise_error(ErrorCode.DETECTION_NOT_FOUND)
    # Refused before the upload, so a full account does not spend the transfer; the link checks again.
    if not saved.has_room(owner, detection_id, payload.size_bytes, get_settings().recording_quota_bytes):
        raise_error(ErrorCode.RECORDING_QUOTA_EXCEEDED)
    return storage.sign(owner, detection_id)


@router.put("/detections/{detection_id}/recording", response_model=RecordingUsage)
def link_recording(detection_id: UUID, payload: RecordingLink, owner: UUID = Depends(authenticate), repo: DetectionRepository = Depends(repository),
                   saved: RecordingRepository = Depends(recordings), storage: RecordingStorage = Depends(clips)) -> RecordingUsage:
    if repo.get_owned(owner, detection_id) is None:
        raise_error(ErrorCode.DETECTION_NOT_FOUND)
    size = storage.verify(owner, detection_id, payload.recording_path)
    saved.link(owner, detection_id, payload.recording_path, size, get_settings().recording_quota_bytes)
    return recording_usage(owner, saved)


@router.get("/detections/{detection_id}/recording", response_model=RecordingUrl)
def recording_url(detection_id: UUID, owner: UUID = Depends(authenticate), saved: RecordingRepository = Depends(recordings),
                  storage: RecordingStorage = Depends(clips)) -> RecordingUrl:
    path = saved.path(owner, detection_id)
    if path is None:
        raise_error(ErrorCode.RECORDING_NOT_FOUND)
    return storage.url(path)


@router.delete("/detections/{detection_id}/recording", status_code=HTTPStatus.NO_CONTENT)
def delete_recording(detection_id: UUID, owner: UUID = Depends(authenticate), saved: RecordingRepository = Depends(recordings),
                     storage: RecordingStorage = Depends(clips)) -> None:
    path = saved.path(owner, detection_id)
    if path is None:
        return
    # The object goes first: if Storage fails the link stays and the request can be repeated.
    storage.delete([path])
    saved.unlink(owner, detection_id)


@router.get("/detections", response_model=MapResponse)
def detections(query: Annotated[MapQuery, Query()], viewer: UUID = Depends(authenticate), repo: DetectionRepository = Depends(repository)) -> MapResponse:
    if query.west > query.east or query.south > query.north:
        raise_error(ErrorCode.INVALID_MAP_BOUNDS)
    if query.since and query.until and query.since >= query.until:
        raise_error(ErrorCode.INVALID_PERIOD)
    return repo.map(viewer, query)


@router.get("/me", response_model=Profile)
def get_profile(owner: UUID = Depends(authenticate), repo: ProfileRepository = Depends(profiles), photos: AvatarStorage = Depends(avatars)) -> Profile:
    stored = repo.get(owner)
    return Profile(alias=stored.alias, avatar_url=photos.url(stored.avatar_path), created_at=stored.created_at)


@router.patch("/me", response_model=Profile)
def update_profile(payload: ProfileInput, owner: UUID = Depends(authenticate), repo: ProfileRepository = Depends(profiles), photos: AvatarStorage = Depends(avatars)) -> Profile:
    if payload.avatar_path is not None:
        photos.verify(owner, payload.avatar_path)
    stored = repo.update(owner, payload.model_dump(include=payload.model_fields_set))
    return Profile(alias=stored.alias, avatar_url=photos.url(stored.avatar_path), created_at=stored.created_at)


@router.post("/me/avatar-url", response_model=AvatarUpload)
def avatar_url(_: AvatarInput, owner: UUID = Depends(authenticate), photos: AvatarStorage = Depends(avatars)) -> AvatarUpload:
    # The declaration is only validated here; the bucket enforces the same type and size on the upload itself.
    return photos.sign(owner)


@router.post("/me/sharing", response_model=SharingUpdate)
def set_sharing(payload: SharingInput, owner: UUID = Depends(authenticate), repo: DetectionRepository = Depends(repository)) -> SharingUpdate:
    # One switch covers what is already in the cloud; detections synchronized later carry their own `shared` value.
    return SharingUpdate(updated=repo.set_sharing(owner, payload.shared))


@router.get("/me/recordings", response_model=RecordingUsage)
def recording_usage(owner: UUID = Depends(authenticate), saved: RecordingRepository = Depends(recordings)) -> RecordingUsage:
    count, size = saved.usage(owner)
    return RecordingUsage(recordings=count, bytes=size, limit_bytes=get_settings().recording_quota_bytes)


@router.delete("/me/recordings", response_model=RecordingsDeleted)
def delete_recordings(owner: UUID = Depends(authenticate), saved: RecordingRepository = Depends(recordings),
                      storage: RecordingStorage = Depends(clips)) -> RecordingsDeleted:
    paths = saved.paths(owner)
    storage.delete(paths)
    return RecordingsDeleted(deleted=saved.unlink_all(owner, paths) if paths else 0)


@router.get("/me/summary", response_model=RecordSummary)
def record_summary(query: Annotated[TimeZoneQuery, Query()], owner: UUID = Depends(authenticate), repo: RecordRepository = Depends(records)) -> RecordSummary:
    return repo.summary(owner, query.tz)


@router.get("/me/species", response_model=OwnSpeciesList)
def own_species(owner: UUID = Depends(authenticate), repo: RecordRepository = Depends(records)) -> OwnSpeciesList:
    return repo.species(owner)


@router.get("/me/species/{species}", response_model=SpeciesRecord)
def species_record(species: Annotated[str, Path(min_length=1, max_length=MAX_SPECIES_LENGTH)], query: Annotated[TimeZoneQuery, Query()],
                   owner: UUID = Depends(authenticate), repo: RecordRepository = Depends(records)) -> SpeciesRecord:
    # A species the caller never recorded is an empty record, not an error: the client shows it as "not yet".
    return repo.species_record(owner, species, query.tz)


@router.get("/sites", response_model=SiteList)
def list_sites(owner: UUID = Depends(authenticate), repo: SiteRepository = Depends(sites)) -> SiteList:
    return SiteList(sites=repo.list_sites(owner))


@router.post("/sites", response_model=Site, status_code=HTTPStatus.CREATED)
def create_site(payload: SiteInput, owner: UUID = Depends(authenticate), repo: SiteRepository = Depends(sites)) -> Site:
    return repo.create_site(owner, payload)


@router.get("/sites/{site_id}/stats", response_model=SiteStats)
def site_stats(site_id: UUID, query: Annotated[StatsQuery, Query()], owner: UUID = Depends(authenticate), repo: SiteRepository = Depends(sites), current: datetime = Depends(now)) -> SiteStats:
    days = query.period.days
    since = current - timedelta(days=days) if days is not None else None
    previous = since - timedelta(days=days) if since is not None and days is not None else None
    return repo.stats(owner, site_id, query.period, since, previous, current, query.tz)


@router.get("/export")
def export(query: Annotated[ExportQuery, Query()], owner: UUID = Depends(authenticate), repo: SiteRepository = Depends(sites)) -> Response:
    if query.since and query.until and query.since >= query.until:
        raise_error(ErrorCode.INVALID_PERIOD)
    body, truncated = repo.export_csv(owner, query.site_id, query.since, query.until)
    filename = EXPORT_FILENAME_TEMPLATE.format(site_id=query.site_id)
    headers = {"Content-Disposition": f'attachment; filename="{filename}"', TRUNCATED_HEADER: str(truncated).lower()}
    return Response(content=body, media_type=EXPORT_MEDIA_TYPE, headers=headers)


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/model/latest")
def latest_model() -> dict[str, Any]:
    return load_manifest(get_settings())


# Routes are copied when the router is included, so this must follow every route definition above.
app.include_router(router)
