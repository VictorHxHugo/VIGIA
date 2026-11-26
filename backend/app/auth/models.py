"""Framework-independent authentication domain models."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from uuid import UUID


class UserRole(str, Enum):
    """Roles assigned by the server, never accepted from API input."""

    VIEWER = "viewer"
    ANALYST = "analyst"
    INCIDENT_OPERATOR = "incident_operator"
    ADMIN = "admin"


def normalize_login(login: str) -> str:
    """Return the canonical, case-insensitive form of a login."""
    if not isinstance(login, str):
        raise ValueError("login must be a string.")

    normalized = login.strip().casefold()
    if not normalized:
        raise ValueError("login must be a non-empty string.")
    return normalized


def validate_session_token_hash(token_hash: str) -> str:
    """Validate a lowercase hexadecimal SHA-256 session-token digest."""
    if (
        not isinstance(token_hash, str)
        or len(token_hash) != 64
        or any(character not in "0123456789abcdef" for character in token_hash)
    ):
        raise ValueError(
            "token_hash must be a 64-character lowercase hexadecimal digest."
        )
    return token_hash


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"{field_name} must be a datetime.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone.")
    return value.astimezone(timezone.utc)


def _as_uuid(value: UUID | str, field_name: str) -> UUID:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError as exc:
            raise ValueError(f"{field_name} must be a valid UUID.") from exc
    raise ValueError(f"{field_name} must be a UUID.")


@dataclass(frozen=True, slots=True)
class User:
    """Persisted user identity and password-hash placeholder for auth services."""

    user_id: UUID
    login: str
    password_hash: str = field(repr=False)
    role: UserRole = UserRole.VIEWER
    is_active: bool = True
    created_at: datetime = field(default_factory=_utc_now)
    updated_at: datetime = field(default_factory=_utc_now)

    def __post_init__(self) -> None:
        object.__setattr__(self, "user_id", _as_uuid(self.user_id, "user_id"))

        normalize_login(self.login)
        object.__setattr__(self, "login", self.login.strip())

        if not isinstance(self.password_hash, str) or not self.password_hash.strip():
            raise ValueError("password_hash must be a non-empty string.")

        try:
            object.__setattr__(self, "role", UserRole(self.role))
        except (TypeError, ValueError) as exc:
            allowed = ", ".join(role.value for role in UserRole)
            raise ValueError(f"role must be one of: {allowed}.") from exc

        if not isinstance(self.is_active, bool):
            raise ValueError("is_active must be a boolean.")

        object.__setattr__(
            self,
            "created_at",
            _as_utc(self.created_at, "created_at"),
        )
        object.__setattr__(
            self,
            "updated_at",
            _as_utc(self.updated_at, "updated_at"),
        )

    @property
    def normalized_login(self) -> str:
        """Return the canonical login used for lookups and uniqueness."""
        return normalize_login(self.login)


@dataclass(frozen=True, slots=True)
class Principal:
    """Minimal authenticated identity passed from the API boundary."""

    user_id: UUID
    role: UserRole

    def __post_init__(self) -> None:
        object.__setattr__(self, "user_id", _as_uuid(self.user_id, "user_id"))
        try:
            object.__setattr__(self, "role", UserRole(self.role))
        except (TypeError, ValueError) as exc:
            allowed = ", ".join(role.value for role in UserRole)
            raise ValueError(f"role must be one of: {allowed}.") from exc


@dataclass(frozen=True, slots=True)
class Session:
    """Server-side session record; contains only a token digest."""

    token_hash: str = field(repr=False)
    user_id: UUID
    expires_at: datetime
    created_at: datetime = field(default_factory=_utc_now)
    revoked_at: datetime | None = None

    def __post_init__(self) -> None:
        validate_session_token_hash(self.token_hash)
        object.__setattr__(self, "user_id", _as_uuid(self.user_id, "user_id"))
        object.__setattr__(
            self,
            "created_at",
            _as_utc(self.created_at, "created_at"),
        )
        object.__setattr__(
            self,
            "expires_at",
            _as_utc(self.expires_at, "expires_at"),
        )
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must be later than created_at.")

        if self.revoked_at is not None:
            revoked_at = _as_utc(self.revoked_at, "revoked_at")
            if revoked_at < self.created_at:
                raise ValueError("revoked_at cannot precede created_at.")
            object.__setattr__(self, "revoked_at", revoked_at)

    def is_valid_at(self, instant: datetime) -> bool:
        """Return whether the session is unrevoked and not yet expired."""
        instant_utc = _as_utc(instant, "instant")
        return (
            self.revoked_at is None
            and self.created_at <= instant_utc < self.expires_at
        )

    def is_expired_at(self, instant: datetime) -> bool:
        """Return whether the session has reached its expiration time."""
        instant_utc = _as_utc(instant, "instant")
        return instant_utc >= self.expires_at
