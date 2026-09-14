"""
Dashboard accounts: signing in, and the admin screens that manage them.

Accounts exist for the web dashboard only. Trackers never authenticate this
way -- a GT06 identifies itself by IMEI over TCP and has no concept of a
user.
"""

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Security, status
from fastapi.security import HTTPAuthorizationCredentials

from ..config import ApiConfig
from ..deps import (
    bearer_scheme,
    current_user,
    get_config,
    get_repository,
    get_users,
    require_admin,
)
from ..repository import LocationRepository
from ..schemas import (
    LoginRequest,
    LoginResponse,
    SelfUpdate,
    SignupRequest,
    UserCreate,
    UserOut,
    UserUpdate,
)
from ..users_repository import AuthenticatedUser, UserRepository

router = APIRouter(tags=["accounts"])

_BAD_CREDENTIALS = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Incorrect username or password",
    headers={"WWW-Authenticate": "Bearer"},
)


@router.post(
    "/auth/login",
    response_model=LoginResponse,
    summary="Sign in and get a session token",
    response_description="A bearer token, when it expires, and the account it belongs to.",
    responses={401: {"description": "Wrong credentials, or the account is deactivated."}},
)
async def login(
    payload: LoginRequest,
    users: UserRepository = Depends(get_users),
    config: ApiConfig = Depends(get_config),
) -> LoginResponse:
    """
    Exchange a username and password for a token.

    Send it back as `Authorization: Bearer <token>`.

    A deactivated account fails here with the same message as a wrong
    password. Saying "this account is deactivated" would confirm to an
    outsider that the username exists.
    """
    started = await users.start_session(
        payload.username, payload.password, timedelta(hours=config.session_ttl_hours)
    )
    if started is None:
        raise _BAD_CREDENTIALS
    token, expires_at, user = started
    return LoginResponse(token=token, expires_at=expires_at, user=user)


async def claim_or_conflict(
    device_id: str,
    user_id: int,
    users: UserRepository,
    repo: LocationRepository,
) -> None:
    """
    Take ownership of a device, or explain why not.

    Shared by signup and by an existing account adding a second vehicle --
    the rules must not drift between the two doors into the same table.

    Requiring the device to have reported already is what stops an IMEI
    range being claimed before the hardware ships: an attacker guessing
    sequential IMEIs can only reach devices that are powered on and
    transmitting, and each of those is a device somebody is holding.
    """
    seen = await repo.list_devices(device_ids=frozenset({device_id}))
    if not seen:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"No tracker with id {device_id} has reported yet. Switch the device on, "
                "wait for it to get a signal, and try again."
            ),
        )
    if not await users.claim_device(user_id, device_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This device is already registered to an account. Contact support to move it.",
        )


@router.post(
    "/auth/signup",
    response_model=LoginResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an account and claim a device with it",
    response_description="The new account, signed in -- same body as /auth/login.",
    responses={
        404: {"description": "That device has never reported a position."},
        409: {"description": "Username or email is taken, or the device already has an owner."},
    },
)
async def signup(
    payload: SignupRequest,
    users: UserRepository = Depends(get_users),
    repo: LocationRepository = Depends(get_repository),
    config: ApiConfig = Depends(get_config),
) -> LoginResponse:
    """
    Self-service onboarding: one call creates the account, claims the tracker
    and signs the person in, so they land on a dashboard already showing
    their vehicle.

    There is no activation code -- first claim wins -- so two rules do the
    work instead: the device must already have reported a position, and a
    device that already has an owner cannot be claimed again. An admin
    releases a device to hand it to a new owner.

    The account is always a plain `user`; signup cannot mint an admin.
    """
    if await users.username_taken(payload.username):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Username '{payload.username}' is already taken",
        )
    if payload.email and await users.email_taken(payload.email):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="That email already has an account"
        )

    created = await users.create_user(
        UserCreate(
            username=payload.username,
            password=payload.password,
            full_name=payload.full_name,
            email=payload.email,
            mobile=payload.mobile,
            role="user",
            devices=[],
        )
    )
    # The account has to exist before the claim can be attributed to it, but
    # a device that turns out to be unclaimable must not leave a usable
    # account behind with no vehicle and no way to have gotten one --
    # someone could sign up again with a fresh device once it is actually
    # theirs, and would otherwise find the username already taken by their
    # own failed attempt.
    try:
        await claim_or_conflict(payload.device_id, created.id, users, repo)
    except HTTPException:
        await users.delete_user(created.id)
        raise

    started = await users.start_session(
        payload.username, payload.password, timedelta(hours=config.session_ttl_hours)
    )
    if started is None:  # pragma: no cover -- the account was just created
        raise _BAD_CREDENTIALS
    token, expires_at, user = started
    return LoginResponse(token=token, expires_at=expires_at, user=user)


@router.post(
    "/auth/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="End the current session",
    response_description="The token is no longer valid.",
)
async def logout(
    credentials: HTTPAuthorizationCredentials | None = Security(bearer_scheme),
    users: UserRepository = Depends(get_users),
) -> None:
    """
    Forget the caller's token.

    Deliberately never fails: signing out an already-invalid token is the
    outcome the caller wanted either way.
    """
    if credentials and credentials.credentials:
        await users.end_session(credentials.credentials)


@router.get(
    "/auth/me",
    response_model=UserOut,
    summary="The signed-in account",
    responses={401: {"description": "Missing, expired or revoked token."}},
)
async def me(
    user: AuthenticatedUser = Depends(current_user),
    users: UserRepository = Depends(get_users),
) -> UserOut:
    """
    Who the current token belongs to.

    A dashboard calls this on load to decide whether a stored token is still
    good, rather than discovering it is not on the first real request.
    """
    found = await users.get_user(user.id)
    if found is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account no longer exists")
    return found


@router.patch(
    "/auth/me",
    response_model=UserOut,
    summary="Edit the signed-in account",
    responses={
        403: {"description": "current_password is missing or wrong."},
        409: {"description": "That email already has an account."},
    },
)
async def update_me(
    payload: SelfUpdate,
    user: AuthenticatedUser = Depends(current_user),
    users: UserRepository = Depends(get_users),
) -> UserOut:
    """
    Self-service profile editing -- full name, email, mobile, and the
    account's own password. Deliberately narrower than an admin's `PATCH
    /users/{id}`: there is no `role`, `is_active` or `devices` here, so this
    can never promote, reactivate or re-scope the caller's own account.

    Changing the password requires `current_password` -- proof the caller
    still is who the session claims, the same reasoning a bank re-asks for
    a password before changing one. It signs out every session on the
    account, this one included, so the next request needs a fresh login.
    """
    if payload.password is not None:
        if not payload.current_password or not await users.verify_current_password(
            user.id, payload.current_password
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="Current password is incorrect"
            )

    if payload.email:
        current = await users.get_user(user.id)
        is_new_email = current is None or (current.email or "").lower() != payload.email.lower()
        if is_new_email and await users.email_taken(payload.email):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="That email already has an account"
            )

    updated = await users.update_user(
        user.id,
        UserUpdate(
            full_name=payload.full_name,
            email=payload.email,
            mobile=payload.mobile,
            password=payload.password,
        ),
    )
    if updated is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account no longer exists")
    return updated


@router.get(
    "/users",
    response_model=list[UserOut],
    dependencies=[Depends(require_admin)],
    summary="List every account",
    response_description="Accounts ordered by username, deactivated ones included.",
    responses={403: {"description": "The caller is not an administrator."}},
)
async def list_users(users: UserRepository = Depends(get_users)) -> list[UserOut]:
    """
    Every account, active or not.

    Deactivated accounts are listed rather than hidden -- an admin needs to
    see them in order to reactivate one.
    """
    return await users.list_users()


@router.post(
    "/users",
    response_model=UserOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin)],
    summary="Create an account",
    responses={
        403: {"description": "The caller is not an administrator."},
        409: {"description": "That username is taken."},
    },
)
async def create_user(
    payload: UserCreate, users: UserRepository = Depends(get_users)
) -> UserOut:
    """
    Add an account.

    Usernames are compared case-insensitively, so `Dispatcher` and
    `dispatcher` cannot both exist. Assigned devices are ignored for an
    admin, which sees the whole fleet regardless.
    """
    if await users.username_taken(payload.username):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Username '{payload.username}' is already taken",
        )
    return await users.create_user(payload)


@router.patch(
    "/users/{user_id}",
    response_model=UserOut,
    summary="Edit, deactivate or reactivate an account",
    responses={
        400: {"description": "The change would leave no active administrator."},
        403: {"description": "The caller is not an administrator."},
        404: {"description": "No such account."},
    },
)
async def update_user(
    user_id: int,
    payload: UserUpdate,
    admin: AuthenticatedUser = Depends(require_admin),
    users: UserRepository = Depends(get_users),
) -> UserOut:
    """
    Change one account. Only the fields you send are touched.

    Deactivating is `is_active: false` rather than a delete, so the account
    can be restored and its history stays intelligible. It takes effect at
    once: the account's tokens are dropped, and every later request re-checks
    the flag.

    Two things are refused. An admin cannot deactivate or demote themselves,
    which is nearly always a misclick and locks them out of this very screen.
    And the last active admin cannot be removed by anyone, because there
    would then be no way to administer anything.
    """
    target = await users.get_user(user_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such account")

    losing_admin = (payload.is_active is False) or (payload.role == "user")
    if losing_admin and target.role == "admin":
        if target.id == admin.id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="You cannot deactivate or demote your own admin account",
            )
        if await users.count_active_admins(excluding=user_id) == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="This is the last active administrator",
            )

    updated = await users.update_user(user_id, payload)
    if updated is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such account")
    return updated
