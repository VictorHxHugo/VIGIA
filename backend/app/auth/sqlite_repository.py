"""SQLite persistence adapter for authentication users and sessions."""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from uuid import UUID

from app.auth.models import (
    Session,
    User,
    UserRole,
    normalize_login,
    validate_session_token_hash,
)
from app.auth.repository import (
    AuthRepository,
    SessionAlreadyExistsError,
    UserAlreadyExistsError,
    UserNotFoundError,
)


_ROLE_VALUES_SQL = ", ".join(f"'{role.value}'" for role in UserRole)


class SQLiteAuthRepository:
    """Store users and sessions in a caller-configured file-backed SQLite DB."""

    def __init__(
        self,
        database_path: str | Path,
        busy_timeout_ms: int = 5000,
    ) -> None:
        if (
            isinstance(busy_timeout_ms, bool)
            or not isinstance(busy_timeout_ms, int)
            or busy_timeout_ms < 0
        ):
            raise ValueError("busy_timeout_ms must be a non-negative integer.")

        self.database_path = str(database_path)
        if not self.database_path.strip():
            raise ValueError("database_path must not be empty.")
        if self.database_path == ":memory:":
            raise ValueError(
                "A file-backed database path is required because connections "
                "are opened per operation."
            )

        self.busy_timeout_ms = busy_timeout_ms
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize_database()

    def create_user(self, user: User) -> User:
        """Create a user; the caller must provide an already-derived hash."""
        try:
            with self._write_transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO auth_users (
                        user_id, login, normalized_login, password_hash,
                        role, is_active, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(user.user_id),
                        user.login,
                        user.normalized_login,
                        user.password_hash,
                        user.role.value,
                        int(user.is_active),
                        self._datetime_to_text(user.created_at),
                        self._datetime_to_text(user.updated_at),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            if "auth_users.normalized_login" in str(exc):
                raise UserAlreadyExistsError(user.normalized_login) from exc
            raise

        return user

    def get_user(self, user_id: UUID) -> User | None:
        """Return a user by stable identifier."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM auth_users WHERE user_id = ?",
                (str(user_id),),
            ).fetchone()
        return self._row_to_user(row) if row is not None else None

    def get_user_by_normalized_login(
        self,
        normalized_login: str,
    ) -> User | None:
        """Return a user by case-insensitive normalized login."""
        normalized = normalize_login(normalized_login)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM auth_users WHERE normalized_login = ?",
                (normalized,),
            ).fetchone()
        return self._row_to_user(row) if row is not None else None

    def create_session(self, session: Session) -> Session:
        """Persist a session record without ever receiving its raw token."""
        try:
            with self._write_transaction() as connection:
                user_exists = connection.execute(
                    "SELECT 1 FROM auth_users WHERE user_id = ?",
                    (str(session.user_id),),
                ).fetchone()
                if user_exists is None:
                    raise UserNotFoundError(session.user_id)

                connection.execute(
                    """
                    INSERT INTO auth_sessions (
                        token_hash, user_id, created_at, expires_at, revoked_at
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        session.token_hash,
                        str(session.user_id),
                        self._datetime_to_text(session.created_at),
                        self._datetime_to_text(session.expires_at),
                        (
                            self._datetime_to_text(session.revoked_at)
                            if session.revoked_at is not None
                            else None
                        ),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            if "auth_sessions.token_hash" in str(exc):
                raise SessionAlreadyExistsError() from exc
            raise

        return session

    def get_session_by_token_hash(self, token_hash: str) -> Session | None:
        """Return the session record, even when expired or revoked."""
        validated_hash = validate_session_token_hash(token_hash)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM auth_sessions WHERE token_hash = ?",
                (validated_hash,),
            ).fetchone()
        return self._row_to_session(row) if row is not None else None

    def revoke_session(
        self,
        token_hash: str,
        revoked_at: datetime,
    ) -> bool:
        """Revoke an unrevoked session and report whether it changed."""
        validated_hash = validate_session_token_hash(token_hash)
        revoked_at_text = self._datetime_to_text(revoked_at)
        with self._write_transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE auth_sessions
                SET revoked_at = ?
                WHERE token_hash = ? AND revoked_at IS NULL
                """,
                (revoked_at_text, validated_hash),
            )
        return cursor.rowcount > 0

    def revoke_user_sessions(
        self,
        user_id: UUID,
        revoked_at: datetime,
    ) -> int:
        """Revoke every unrevoked session belonging to a user."""
        revoked_at_text = self._datetime_to_text(revoked_at)
        with self._write_transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE auth_sessions
                SET revoked_at = ?
                WHERE user_id = ? AND revoked_at IS NULL
                """,
                (revoked_at_text, str(user_id)),
            )
        return cursor.rowcount

    def _initialize_database(self) -> None:
        with self._write_transaction() as connection:
            connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS auth_users (
                    user_id TEXT PRIMARY KEY
                        CHECK (length(user_id) = 36),
                    login TEXT NOT NULL
                        CHECK (length(trim(login)) > 0),
                    normalized_login TEXT NOT NULL UNIQUE
                        CHECK (length(trim(normalized_login)) > 0),
                    password_hash TEXT NOT NULL
                        CHECK (length(trim(password_hash)) > 0),
                    role TEXT NOT NULL
                        CHECK (role IN ({_ROLE_VALUES_SQL})),
                    is_active INTEGER NOT NULL
                        CHECK (is_active IN (0, 1)),
                    created_at TEXT NOT NULL
                        CHECK (length(created_at) > 0),
                    updated_at TEXT NOT NULL
                        CHECK (length(updated_at) > 0)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS auth_sessions (
                    token_hash TEXT PRIMARY KEY
                        CHECK (
                            length(token_hash) = 64
                            AND token_hash NOT GLOB '*[^0-9a-f]*'
                        ),
                    user_id TEXT NOT NULL
                        REFERENCES auth_users(user_id) ON DELETE CASCADE,
                    created_at TEXT NOT NULL
                        CHECK (julianday(created_at) IS NOT NULL),
                    expires_at TEXT NOT NULL
                        CHECK (
                            julianday(expires_at) IS NOT NULL
                            AND julianday(expires_at) > julianday(created_at)
                        ),
                    revoked_at TEXT
                        CHECK (
                            revoked_at IS NULL
                            OR (
                                julianday(revoked_at) IS NOT NULL
                                AND julianday(revoked_at) >= julianday(created_at)
                            )
                        )
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS ix_auth_sessions_user_status_expiry
                ON auth_sessions (user_id, revoked_at, expires_at)
                """
            )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._open_connection()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._open_connection()
        try:
            try:
                connection.execute("BEGIN IMMEDIATE")
            except BaseException as original_error:
                self._rollback_after_error(connection, original_error)
                raise

            try:
                yield connection
            except BaseException as original_error:
                self._rollback_after_error(connection, original_error)
                raise

            try:
                connection.commit()
            except BaseException as original_error:
                self._rollback_after_error(connection, original_error)
                raise
        finally:
            connection.close()

    @staticmethod
    def _rollback_after_error(
        connection: sqlite3.Connection,
        original_error: BaseException,
    ) -> None:
        if not connection.in_transaction:
            return
        try:
            connection.rollback()
        except BaseException as rollback_error:
            raise rollback_error from original_error

    def _open_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=self.busy_timeout_ms / 1000,
        )
        try:
            connection.row_factory = sqlite3.Row
            connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
            connection.execute("PRAGMA foreign_keys = ON")
        except BaseException as original_error:
            try:
                connection.close()
            except BaseException as close_error:
                raise close_error from original_error
            raise
        return connection

    @classmethod
    def _row_to_user(cls, row: sqlite3.Row) -> User:
        return User(
            user_id=UUID(row["user_id"]),
            login=row["login"],
            password_hash=row["password_hash"],
            role=UserRole(row["role"]),
            is_active=bool(row["is_active"]),
            created_at=cls._text_to_datetime(row["created_at"]),
            updated_at=cls._text_to_datetime(row["updated_at"]),
        )

    @classmethod
    def _row_to_session(cls, row: sqlite3.Row) -> Session:
        return Session(
            token_hash=row["token_hash"],
            user_id=UUID(row["user_id"]),
            created_at=cls._text_to_datetime(row["created_at"]),
            expires_at=cls._text_to_datetime(row["expires_at"]),
            revoked_at=(
                cls._text_to_datetime(row["revoked_at"])
                if row["revoked_at"] is not None
                else None
            ),
        )

    @staticmethod
    def _datetime_to_text(value: datetime) -> str:
        if not isinstance(value, datetime):
            raise ValueError("Timestamp must be a datetime.")
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Timestamp must include a timezone.")
        return value.astimezone(timezone.utc).isoformat(
            timespec="microseconds"
        )

    @staticmethod
    def _text_to_datetime(value: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("Stored timestamp is not valid ISO-8601.") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("Stored timestamp must include a timezone.")
        return parsed.astimezone(timezone.utc)
