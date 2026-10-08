"""Synchronization, map, profile, monitoring sites, statistics, CSV export and the model manifest."""
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from typing import Annotated, Any, Final
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse, Response

from .auth import authenticate
from .contracts import (
    AudioInput,
    AudioUpload,
    AvatarInput,
    AvatarUpload,
    BatchInput,
    BatchResponse,
    ExportQuery,
    MapQuery,
    MapResponse,
    Profile,
    ProfileInput,
    Site,
    SiteInput,
    SiteList,
    SiteStats,
    StatsQuery,
)
from .domain import DbDetectionStatus, DetectionStatus, local_status
from .errors import ErrorCode, error_response, raise_error
from .manifest import load_manifest
from .profiles import ProfileRepository
from .repository import DetectionRepository
from .settings import SettingsError, get_settings
from .sites import SiteRepository
from .storage import AudioStorage, AvatarStorage

# Version prefix shared by every public route.
API_PREFIX: Final = "/v1"
# Tells the client that the export stopped at EXPORT_ROW_LIMIT rows.
TRUNCATED_HEADER: Final = "X-Truncated"
EXPORT_FILENAME_TEMPLATE: Final = "birdnet-{site_id}.csv"
EXPORT_MEDIA_TYPE: Final = "text/csv; charset=utf-8"
# Methods whose bodies are read into memory and therefore bounded by MAX_METADATA_BODY_BYTES.
BOUNDED_BODY_METHODS: Final = frozenset({"POST", "PATCH"})

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # Report malformed configuration at startup rather than on the first request that reads it.
    get_settings()
    yield


# Deployed as its own project: routes live at the root and the web app proxies /api/* here.
app = FastAPI(title="BirdNet Local API", docs_url=None, redoc_url=None, lifespan=lifespan)
router = APIRouter(prefix=API_PREFIX)


def repository() -> DetectionRepository:
    return DetectionRepository()


def sites() -> SiteRepository:
    return SiteRepository()


def now() -> datetime:
    return datetime.now(UTC)


def storage() -> AudioStorage:
    return AudioStorage()


def profiles() -> ProfileRepository:
    return ProfileRepository()


def avatars() -> AvatarStorage:
    return AvatarStorage()


@app.exception_handler(SettingsError)
async def invalid_configuration(_: Request, error: SettingsError) -> JSONResponse:
    # The message names the variable; it goes to the operator's logs, not to the client.
    logger.error("Invalid configuration: %s", error)
    return error_response(ErrorCode.CONFIGURATION_INVALID)


@app.middleware("http")
async def bounded_body(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    # The API never accepts audio, images or unbounded JSON bodies: files go straight to Storage.
    if request.method in BOUNDED_BODY_METHODS:
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
def batch(payload: BatchInput, owner: UUID = Depends(authenticate), repo: DetectionRepository = Depends(repository), audio: AudioStorage = Depends(storage)) -> BatchResponse:
    for row in payload.detections:
        if row.status != local_status(row.confidence):
            raise_error(ErrorCode.STATUS_CONFIDENCE_MISMATCH)
        if row.audio_path:
            if row.status != DetectionStatus.PROVISIONAL:
                raise_error(ErrorCode.AUDIO_ONLY_FOR_PROVISIONAL)
            if repo.get_owned(owner, row.id) is None:
                raise_error(ErrorCode.DETECTION_NOT_SYNCHRONIZED)
            audio.verify(owner, row.id, row.audio_path)
    return repo.batch(owner, payload.detections)


@router.post("/detections/{detection_id}/audio-url", response_model=AudioUpload)
def audio_url(detection_id: UUID, payload: AudioInput, owner: UUID = Depends(authenticate), repo: DetectionRepository = Depends(repository), audio: AudioStorage = Depends(storage)) -> AudioUpload:
    row = repo.get_owned(owner, detection_id)
    if row is None:
        raise_error(ErrorCode.DETECTION_NOT_FOUND)
    if row["estado"] != DbDetectionStatus.PROVISIONAL:
        raise_error(ErrorCode.UPLOAD_ONLY_FOR_PROVISIONAL)
    return audio.sign(owner, detection_id)


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
