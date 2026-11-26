"""FastAPI authentication and authorization dependencies."""

from collections.abc import Callable, Mapping
from enum import Enum
from types import MappingProxyType
from typing import Annotated

from fastapi import Cookie, Depends, HTTPException, Request, status

from app.auth.models import Principal, UserRole
from app.auth.service import AuthService, InvalidSessionError


class Permission(str, Enum):
    """Server-defined permissions checked at the API boundary."""

    INCIDENT_READ = "incident:read"
    SCHEMA_READ = "schema:read"
    DATASET_ANALYZE = "dataset:analyze"
    INCIDENT_STATUS_CHANGE = "incident:status:change"
    SCHEMA_APPROVE = "schema:approve"


_READ_PERMISSIONS = frozenset(
    {
        Permission.INCIDENT_READ,
        Permission.SCHEMA_READ,
    }
)
_ANALYST_PERMISSIONS = _READ_PERMISSIONS | frozenset(
    {
        Permission.DATASET_ANALYZE,
    }
)
_INCIDENT_OPERATOR_PERMISSIONS = _ANALYST_PERMISSIONS | frozenset(
    {
        Permission.INCIDENT_STATUS_CHANGE,
    }
)

ROLE_PERMISSIONS: Mapping[UserRole, frozenset[Permission]] = MappingProxyType(
    {
        UserRole.VIEWER: _READ_PERMISSIONS,
        UserRole.ANALYST: _ANALYST_PERMISSIONS,
        UserRole.INCIDENT_OPERATOR: _INCIDENT_OPERATOR_PERMISSIONS,
        UserRole.ADMIN: frozenset(Permission),
    }
)

SESSION_COOKIE_NAME = "vigia_session"
_AUTHENTICATION_REQUIRED = "Authentication required."
_INSUFFICIENT_PERMISSION = "Insufficient permissions."


def get_auth_service(request: Request) -> AuthService:
    """Resolve the application-scoped auth service from FastAPI state."""
    auth_service = getattr(request.app.state, "auth_service", None)
    if auth_service is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Authentication service is not configured.",
        )
    return auth_service


def get_current_principal(
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
    session_token: Annotated[
        str | None,
        Cookie(alias=SESSION_COOKIE_NAME),
    ] = None,
) -> Principal:
    """Resolve and validate the session cookie into an authenticated principal."""
    if not session_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_AUTHENTICATION_REQUIRED,
        )

    try:
        return auth_service.validate_session(session_token)
    except InvalidSessionError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_AUTHENTICATION_REQUIRED,
        ) from exc


def require_permission(
    permission: Permission | str,
) -> Callable[..., Principal]:
    """Create a dependency that requires a server-defined permission."""
    try:
        required_permission = Permission(permission)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Unknown permission: {permission!r}.") from exc

    def permission_dependency(
        principal: Annotated[Principal, Depends(get_current_principal)],
    ) -> Principal:
        allowed_permissions = ROLE_PERMISSIONS.get(
            principal.role,
            frozenset(),
        )
        if required_permission not in allowed_permissions:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=_INSUFFICIENT_PERMISSION,
            )
        return principal

    return permission_dependency


__all__ = [
    "Permission",
    "ROLE_PERMISSIONS",
    "SESSION_COOKIE_NAME",
    "get_auth_service",
    "get_current_principal",
    "require_permission",
]
