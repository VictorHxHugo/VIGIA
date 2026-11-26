import sqlite3
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from uuid import uuid4

import pytest

from app.auth.models import Session, User, UserRole
from app.auth.repository import (
    SessionAlreadyExistsError,
    UserAlreadyExistsError,
    UserNotFoundError,
)
from app.auth.sqlite_repository import SQLiteAuthRepository


NOW = datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc)


def make_user(
    *,
    user_id=None,
    login: str = "Victor",
    role: UserRole = UserRole.ANALYST,
) -> User:
    return User(
        user_id=user_id or uuid4(),
        login=login,
        password_hash="opaque-test-password-hash",
        role=role,
        created_at=NOW,
        updated_at=NOW,
    )


def make_session(
    user_id,
    *,
    token: str = "session-token",
    expires_at: datetime = NOW + timedelta(hours=1),
    created_at: datetime = NOW,
) -> Session:
    return Session(
        token_hash=sha256(token.encode("utf-8")).hexdigest(),
        user_id=user_id,
        created_at=created_at,
        expires_at=expires_at,
    )


@pytest.fixture
def repository(tmp_path):
    return SQLiteAuthRepository(tmp_path / "nested" / "auth.sqlite3")


def test_creates_and_reconstructs_user_by_id_and_normalized_login(repository):
    user = make_user(login="  Victor@Example.Test ")

    assert repository.create_user(user) == user
    assert repository.get_user(user.user_id) == user
    assert repository.get_user_by_normalized_login(
        "VICTOR@EXAMPLE.TEST"
    ) == user


def test_normalized_login_is_unique(repository):
    repository.create_user(make_user(login="Victor"))

    with pytest.raises(UserAlreadyExistsError):
        repository.create_user(make_user(login="  VICTOR "))


def test_creates_session_and_persists_only_its_hash(repository):
    user = repository.create_user(make_user())
    raw_token = "one-time-test-session-token"
    token_hash = sha256(raw_token.encode("utf-8")).hexdigest()
    session = make_session(user.user_id, token=raw_token)

    assert repository.create_session(session) == session
    assert repository.get_session_by_token_hash(token_hash) == session

    with sqlite3.connect(repository.database_path) as connection:
        stored_hash = connection.execute(
            "SELECT token_hash FROM auth_sessions WHERE user_id = ?",
            (str(user.user_id),),
        ).fetchone()[0]
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(auth_sessions)")
        }

    assert stored_hash == token_hash
    assert raw_token not in stored_hash
    assert columns == {
        "token_hash",
        "user_id",
        "created_at",
        "expires_at",
        "revoked_at",
    }


def test_duplicate_session_hash_is_rejected_and_connection_remains_usable(
    repository,
):
    user = repository.create_user(make_user())
    session = make_session(user.user_id)
    repository.create_session(session)

    with pytest.raises(SessionAlreadyExistsError):
        repository.create_session(session)

    assert repository.get_session_by_token_hash(session.token_hash) == session


def test_session_expiration_and_revocation_are_preserved(repository):
    user = repository.create_user(make_user())
    expires_at = NOW + timedelta(minutes=5)
    session = make_session(user.user_id, expires_at=expires_at)
    repository.create_session(session)

    loaded = repository.get_session_by_token_hash(session.token_hash)
    assert loaded is not None
    assert loaded.is_valid_at(NOW)
    assert not loaded.is_valid_at(expires_at)

    revoked_at = NOW + timedelta(minutes=1)
    assert repository.revoke_session(session.token_hash, revoked_at)
    assert not repository.revoke_session(session.token_hash, revoked_at)

    revoked = repository.get_session_by_token_hash(session.token_hash)
    assert revoked is not None
    assert revoked.revoked_at == revoked_at
    assert not revoked.is_valid_at(NOW)


def test_revokes_all_unrevoked_sessions_for_one_user(repository):
    first_user = repository.create_user(make_user())
    second_user = repository.create_user(make_user(login="other-user"))
    first_session = make_session(first_user.user_id, token="first")
    second_session = make_session(first_user.user_id, token="second")
    other_session = make_session(second_user.user_id, token="other")
    for session in (first_session, second_session, other_session):
        repository.create_session(session)

    revoked_at = NOW + timedelta(minutes=1)
    assert repository.revoke_user_sessions(first_user.user_id, revoked_at) == 2
    assert repository.revoke_user_sessions(first_user.user_id, revoked_at) == 0

    assert repository.get_session_by_token_hash(
        first_session.token_hash
    ).revoked_at == revoked_at
    assert repository.get_session_by_token_hash(
        second_session.token_hash
    ).revoked_at == revoked_at
    assert repository.get_session_by_token_hash(
        other_session.token_hash
    ).revoked_at is None


def test_session_requires_an_existing_user(repository):
    session = make_session(uuid4())

    with pytest.raises(UserNotFoundError):
        repository.create_session(session)

    assert repository.get_session_by_token_hash(session.token_hash) is None


def test_sessions_reference_users_and_cascade_on_user_deletion(repository):
    user = repository.create_user(make_user())
    session = make_session(user.user_id)
    repository.create_session(session)

    with sqlite3.connect(repository.database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "DELETE FROM auth_users WHERE user_id = ?",
            (str(user.user_id),),
        )

    assert repository.get_session_by_token_hash(session.token_hash) is None


def test_repository_closes_connections_after_operations(tmp_path, monkeypatch):
    import app.auth.sqlite_repository as sqlite_repository_module

    opened_connections = []
    original_connect = sqlite_repository_module.sqlite3.connect

    def track_connection(*args, **kwargs):
        connection = original_connect(*args, **kwargs)
        opened_connections.append(connection)
        return connection

    monkeypatch.setattr(
        sqlite_repository_module.sqlite3,
        "connect",
        track_connection,
    )

    repository = SQLiteAuthRepository(tmp_path / "auth.sqlite3")
    repository.create_user(make_user())
    repository.get_user_by_normalized_login("victor")
    with pytest.raises(UserAlreadyExistsError):
        repository.create_user(make_user(login="VICTOR"))

    assert opened_connections
    for connection in opened_connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            connection.execute("SELECT 1")
