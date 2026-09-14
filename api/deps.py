"""Shared FastAPI dependencies."""

from fastapi import Depends, HTTPException, Request, Security, status
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

from .config import ApiConfig
from .repository import LocationRepository
from .state import ensure_repository, ensure_users
from .users_repository import AuthenticatedUser, UserRepository

# Declared as a security scheme rather than a plain Header parameter so that
# Swagger UI renders an "Authorize" button and sends the key on every call.
ingest_key_scheme = APIKeyHeader(
    name="X-API-Key",
    auto_error=False,  # handled below, so the disabled case can return 503
    description="Shared secret for HTTP ingest. Set INGEST_API_KEY on the server.",
)


def get_config(request: Request) -> ApiConfig:
    return request.app.state.config


async def get_repository(request: Request) -> LocationRepository:
    """Built on first use, so the API works without ASGI lifespan support."""
    return await ensure_repository(request.app)


def require_ingest_key(
    api_key: str | None = Security(ingest_key_scheme),
    config: ApiConfig = Depends(get_config),
) -> None:
    """
    Guard the HTTP ingest endpoint.

    With no key configured the endpoint refuses every request rather than
    accepting anonymous writes -- an unauthenticated ingest path lets anyone
    forge a device's position.
    """
    if not config.ingest_api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="HTTP ingest is disabled; set INGEST_API_KEY to enable it",
        )
    if api_key != config.ingest_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing X-API-Key"
        )


# --- dashboard accounts -----------------------------------------------------

# auto_error=False so a missing header produces our own 401 with a
# WWW-Authenticate hint, rather than FastAPI's bare 403.
bearer_scheme = HTTPBearer(
    auto_error=False,
    description="Session token from POST /api/v1/auth/login.",
)

_UNAUTHENTICATED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Sign in to use this endpoint",
    headers={"WWW-Authenticate": "Bearer"},
)


async def get_users(request: Request) -> UserRepository:
    """Built on first use, like the location repository, and sharing its pool."""
    return await ensure_users(request.app)


async def current_user(
    credentials: HTTPAuthorizationCredentials | None = Security(bearer_scheme),
    users: UserRepository = Depends(get_users),
) -> AuthenticatedUser:
    """
    The signed-in account, or 401.

    The token is resolved against storage on every request rather than being
    trusted on its own. That is what makes deactivation immediate: a token
    issued a minute ago stops working the moment its account is switched off.
    """
    if credentials is None or not credentials.credentials:
        raise _UNAUTHENTICATED
    user = await users.resolve_token(credentials.credentials)
    if user is None:
        raise _UNAUTHENTICATED
    return user


async def require_admin(user: AuthenticatedUser = Depends(current_user)) -> AuthenticatedUser:
    """
    Admin-only endpoints.

    403 rather than 401: the caller is authenticated and re-authenticating
    will not help, which is the distinction a client needs in order to decide
    between showing a sign-in form and showing "not allowed".
    """
    if not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="This endpoint is for administrators"
        )
    return user
