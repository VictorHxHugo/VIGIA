"""Persistence contract for users and server-side sessions."""

from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.auth.models import Session, User


class UserAlreadyExistsError(Exception):
    """Raised when a user login is already registered."""

    def __init__(self, normalized_login: str) -> None:
        self.normalized_login = normalized_login
        super().__init__(f"A user with login '{normalized_login}' already exists.")


class UserNotFoundError(Exception):
    """Raised when a session references a user that does not exist."""

    def __init__(self, user_id: UUID) -> None:
        self.user_id = user_id
        super().__init__(f"User '{user_id}' was not found.")


class SessionAlreadyExistsError(Exception):
    """Raised when a session token hash is already stored."""

    def __init__(self) -> None:
        super().__init__("A session with this token hash already exists.")


class AuthRepository(Protocol):
    """Persistence operations required by framework-independent auth services."""

    def create_user(self, user: User) -> User:
        """Persist a user with a password hash supplied by the caller."""
        ...

    def get_user(self, user_id: UUID) -> User | None:
        """Return a user by stable identifier, or None when absent."""
        ...

    def get_user_by_normalized_login(
        self,
        normalized_login: str,
    ) -> User | None:
        """Return a user by case-insensitive normalized login, or None."""
        ...

    def create_session(self, session: Session) -> Session:
        """Persist a session containing only the token hash."""
        ...

    def get_session_by_token_hash(self, token_hash: str) -> Session | None:
        """Return a session by token hash, including expired or revoked records."""
        ...

    def revoke_session(
        self,
        token_hash: str,
        revoked_at: datetime,
    ) -> bool:
        """Revoke one session; return False if it is absent or already revoked."""
        ...

    def revoke_user_sessions(
        self,
        user_id: UUID,
        revoked_at: datetime,
    ) -> int:
        """Revoke all unrevoked sessions for a user and return the count."""
        ...
