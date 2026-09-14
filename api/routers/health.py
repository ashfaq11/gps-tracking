from fastapi import APIRouter, Depends

from ..config import ApiConfig
from ..deps import get_config, get_repository
from ..repository import LocationRepository
from ..schemas import HealthOut

router = APIRouter(tags=["health"])


@router.get(
    "/health",
    response_model=HealthOut,
    summary="Liveness and database reachability",
    response_description="status is 'ok', or 'degraded' when the database is unreachable.",
)
async def health(
    config: ApiConfig = Depends(get_config),
    repo: LocationRepository = Depends(get_repository),
) -> HealthOut:
    """
    Always returns 200 if the process is up; read `database` to tell whether
    storage is actually reachable. Suitable for a load-balancer check.
    """
    try:
        database = "ok" if await repo.ping() else "unreachable"
    except Exception:
        database = "unreachable"
    return HealthOut(
        status="ok" if database == "ok" else "degraded",
        backend=config.backend,
        database=database,
    )
