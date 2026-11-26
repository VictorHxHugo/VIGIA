"""Framework-independent authentication application service."""

import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Protocol
from uuid import UUID, uuid4

from pwdlib import PasswordHash

from app.auth.models import (
    Principal,
    Session,
    User,
    UserRole,
    normalize_login,
)
from app.auth.repository import AuthRepository, UserNotFoundError


class InvalidCredentialsError(Exception):
    """Raised when login credentials do not identify an active user."""

    def __init__(self) -> None:
        super().__init__("Invalid login or password.")


class InvalidSessionError(Exception):
    """Raised when a session token is absent, expired, or no longer valid."""

    def __init__(self) -> None:
        super().__init__("Invalid or expired session.")


class PasswordHasher(Protocol):
    """Hashing operations required by the authentication service."""

    def hash(self, password: str) -> str:
        ...

    def verify(self, password: str, hash: str) -> bool:
        ...


@dataclass(frozen=True, slots=True)
class AuthenticatedSession:
    """Login result; the raw session token is returned only once."""

    principal: Principal
    session_token: str = field(repr=False)
    expires_at: datetime


class AuthService:
    """Manage password authentication and opaque server-side sessions."""

    def __init__(
        self,
        repository: AuthRepository,
        *,
        session_lifetime: timedelta = timedelta(hours=8),
        password_hasher: PasswordHasher | None = None,
    ) -> None:
        if (
            not isinstance(session_lifetime, timedelta)
            or session_lifetime <= timedelta(0)
        ):
            raise ValueError("session_lifetime must be a positive timedelta.")

        self._repository = repository
        self._session_lifetime = session_lifetime
        self._password_hasher = (
            password_hasher
            if password_hasher is not None
            else PasswordHash.recommended()
        )
        self._dummy_password_hash = self._password_hasher.hash(
            secrets.token_urlsafe(32)
        )

    def create_user(
        self,
        login: str,
        password: str,
        *,
        role: UserRole = UserRole.VIEWER,
        is_active: bool = True,
        user_id: UUID | None = None,
    ) -> User:
        """Create a user after hashing the supplied password."""
        normalize_login(login)
        self._validate_password(password)

        try:
            assigned_role = UserRole(role)
        except (TypeError, ValueError) as exc:
            allowed = ", ".join(candidate.value for candidate in UserRole)
            raise ValueError(f"role must be one of: {allowed}.") from exc

        if not isinstance(is_active, bool):
            raise ValueError("is_active must be a boolean.")

        user = User(
            user_id=user_id if user_id is not None else uuid4(),
            login=login,
            password_hash=self._password_hasher.hash(password),
            role=assigned_role,
            is_active=is_active,
        )
        return self._repository.create_user(user)

    def authenticate(
        self,
        login: str,
        password: str,
    ) -> AuthenticatedSession:
        """Verify credentials and issue a new opaque session token."""
        if not isinstance(password, str):
            raise InvalidCredentialsError()

        try:
            normalized_login = normalize_login(login)
        except ValueError as exc:
            self._password_hasher.verify(
                password,
                self._dummy_password_hash,
            )
            raise InvalidCredentialsError() from exc

        user = self._repository.get_user_by_normalized_login(normalized_login)
        password_hash = (
            user.password_hash if user is not None else self._dummy_password_hash
        )
        password_matches = self._password_hasher.verify(password, password_hash)

        if user is None or not password_matches or not user.is_active:
            raise InvalidCredentialsError()

        return self._create_session(user)

    def validate_session(self, session_token: str) -> Principal:
        """Validate a raw token and return the active user's principal."""
        if not isinstance(session_token, str) or not session_token:
            raise InvalidSessionError()

        session = self._repository.get_session_by_token_hash(
            self._token_hash(session_token)
        )
        now = self._utc_now()
        if session is None or not session.is_valid_at(now):
            raise InvalidSessionError()

        user = self._repository.get_user(session.user_id)
        if user is None or not user.is_active:
            raise InvalidSessionError()

        return Principal(user_id=user.user_id, role=user.role)

    def logout(self, session_token: str) -> bool:
        """Revoke one session; return False when it is absent or already revoked."""
        if not isinstance(session_token, str):
            raise ValueError("session_token must be a string.")

        return self._repository.revoke_session(
            self._token_hash(session_token),
            self._utc_now(),
        )

    def revoke_user_sessions(self, user_id: UUID) -> int:
        """Revoke every active session belonging to the supplied user."""
        if not isinstance(user_id, UUID):
            raise ValueError("user_id must be a UUID.")

        return self._repository.revoke_user_sessions(
            user_id,
            self._utc_now(),
        )

    def _create_session(self, user: User) -> AuthenticatedSession:
        session_token = secrets.token_urlsafe(32)
        created_at = self._utc_now()
        expires_at = created_at + self._session_lifetime
        session = Session(
            token_hash=self._token_hash(session_token),
            user_id=user.user_id,
            created_at=created_at,
            expires_at=expires_at,
        )
        try:
            self._repository.create_session(session)
        except UserNotFoundError as exc:
            raise InvalidCredentialsError() from exc
        return AuthenticatedSession(
            principal=Principal(user_id=user.user_id, role=user.role),
            session_token=session_token,
            expires_at=expires_at,
        )

    @staticmethod
    def _validate_password(password: str) -> None:
        if not isinstance(password, str) or not password:
            raise ValueError("password must be a non-empty string.")

    @staticmethod
    def _token_hash(session_token: str) -> str:
        return sha256(session_token.encode("utf-8")).hexdigest()

    @staticmethod
    def _utc_now() -> datetime:
        return datetime.now(timezone.utc)


__all__ = [
    "AuthService",
    "AuthenticatedSession",
    "InvalidCredentialsError",
    "InvalidSessionError",
    "PasswordHasher",
]
