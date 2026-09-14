"""
HTTP ingest.

This is the path for clients that can speak HTTP: a phone app, a backend
integration, or one of the few trackers with an HTTP mode. GT06 hardware
cannot post JSON -- it opens a raw TCP socket to the gateway instead. Both
paths write the same rows, so downstream readers cannot tell them apart.
"""

from fastapi import APIRouter, Depends, status

from ..deps import get_repository, require_ingest_key
from ..repository import LocationRepository
from ..schemas import IngestAccepted, LocationIn

router = APIRouter(prefix="/ingest", tags=["ingest"])


@router.post(
    "/location",
    response_model=IngestAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_ingest_key)],
    summary="Report a position over HTTP",
    response_description="The fix was stored.",
    responses={
        401: {"description": "Missing or wrong X-API-Key."},
        422: {"description": "Payload failed validation, e.g. latitude outside -90..90."},
        503: {"description": "Ingest is disabled because INGEST_API_KEY is not set."},
    },
)
async def ingest_location(
    location: LocationIn,
    repo: LocationRepository = Depends(get_repository),
) -> IngestAccepted:
    """
    Store one position reported by an HTTP client.

    Requires the `X-API-Key` header — click **Authorize** above to set it.

    GT06 trackers cannot reach this endpoint; they send binary frames over TCP
    to the gateway on port 5023. Both paths write the same rows, so readers
    cannot tell which one a fix arrived through.
    """
    await repo.add(location)
    return IngestAccepted(device_id=location.device_id)
