"""FastAPI routes for server-side authentication sessions.

Login and logout do not yet enforce CSRF or Origin checks. Add those protections
before exposing these cookie-authenticated endpoints to production traffic.
"""

import logging
from datetime import datetime, timezone
from typing import Annotated

from fastapi import (
    APIRouter,
    Cookie,
    Depends,
    HTTPException,
    Request,
    Response,
    status,
)
from fastapi.responses import JSONResponse

from app.auth.api_schemas import AuthUserResponse, LoginRequest
from app.auth.dependencies import (
    SESSION_COOKIE_NAME,
    get_auth_service,
    get_current_principal,
)
from app.auth.models import Principal
from app.auth.service import (
    AuthService,
    InvalidCredentialsError,
)


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["authentication"])


def _set_session_cookie(
    response: Response,
    request: Request,
    session_token: str,
    expires_at: datetime,
) -> None:
    remaining_seconds = max(
        0,
        int((expires_at - datetime.now(timezone.utc)).total_seconds()),
    )
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_token,
        max_age=remaining_seconds,
        path="/",
        secure=request.app.state.auth_cookie_secure,
        httponly=True,
        samesite="lax",
    )


def _clear_session_cookie(response: Response, request: Request) -> None:
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path="/",
        secure=request.app.state.auth_cookie_secure,
        httponly=True,
        samesite="lax",
    )


@router.post("/login", response_model=AuthUserResponse)
def login(
    credentials: LoginRequest,
    request: Request,
    response: Response,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
) -> AuthUserResponse:
    """Authenticate credentials and place the opaque token in an HttpOnly cookie."""
    try:
        authenticated_session = auth_service.authenticate(
            credentials.login,
            credentials.password,
        )
    except InvalidCredentialsError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid login or password.",
        ) from exc
    except Exception as exc:
        logger.exception("Authentication login failed.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected authentication error occurred.",
        ) from exc

    _set_session_cookie(
        response,
        request,
        authenticated_session.session_token,
        authenticated_session.expires_at,
    )
    return AuthUserResponse(
        user_id=authenticated_session.principal.user_id,
        role=authenticated_session.principal.role,
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    request: Request,
    response: Response,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
    session_token: Annotated[
        str | None,
        Cookie(alias=SESSION_COOKIE_NAME),
    ] = None,
) -> Response:
    """Revoke the current session when present and always clear the cookie."""
    try:
        if session_token:
            auth_service.logout(session_token)
    except Exception:
        logger.exception("Authentication logout failed.")
        error_response = JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "An unexpected authentication error occurred."},
        )
        _clear_session_cookie(error_response, request)
        return error_response

    _clear_session_cookie(response, request)
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.get("/me", response_model=AuthUserResponse)
def current_user(
    principal: Annotated[Principal, Depends(get_current_principal)],
) -> AuthUserResponse:
    """Return the public fields of the authenticated principal."""
    return AuthUserResponse(user_id=principal.user_id, role=principal.role)


__all__ = ["router"]
