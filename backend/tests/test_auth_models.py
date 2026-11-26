from datetime import datetime, timedelta, timezone
from hashlib import sha256
from uuid import uuid4

import pytest

from app.auth.models import (
    Principal,
    Session,
    User,
    UserRole,
    normalize_login,
)


NOW = datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc)


def make_user(**overrides) -> User:
    values = {
        "user_id": uuid4(),
        "login": "Victor",
        "password_hash": "opaque-test-password-hash",
        "role": UserRole.ANALYST,
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(overrides)
    return User(**values)


def make_session(**overrides) -> Session:
    values = {
        "token_hash": sha256(b"session-token").hexdigest(),
        "user_id": uuid4(),
        "created_at": NOW,
        "expires_at": NOW + timedelta(hours=1),
    }
    values.update(overrides)
    return Session(**values)


def test_normalizes_login_by_trimming_and_casefolding():
    assert normalize_login("  Victor@Example.Test ") == "victor@example.test"
    assert make_user(login="  Victor@Example.Test ").normalized_login == (
        "victor@example.test"
    )


def test_user_and_principal_validate_server_defined_roles():
    user = make_user(role="admin")
    principal = Principal(user_id=user.user_id, role="admin")

    assert user.role is UserRole.ADMIN
    assert principal.user_id == user.user_id
    assert principal.role is UserRole.ADMIN

    with pytest.raises(ValueError, match="role"):
        make_user(role="unknown")


def test_user_rejects_blank_login_and_naive_timestamps():
    with pytest.raises(ValueError, match="login"):
        make_user(login="  ")

    with pytest.raises(ValueError, match="timezone"):
        make_user(created_at=datetime(2026, 1, 2, 3, 4))


def test_user_normalizes_timestamps_to_utc():
    offset = timezone(timedelta(hours=-3))
    user = make_user(
        created_at=NOW.astimezone(offset),
        updated_at=NOW.astimezone(offset),
    )

    assert user.created_at == NOW
    assert user.updated_at == NOW
    assert user.created_at.tzinfo is timezone.utc


def test_session_hash_is_required_and_session_expiry_is_enforced():
    session = make_session()

    assert not session.is_valid_at(NOW - timedelta(microseconds=1))
    assert session.is_valid_at(NOW)
    assert not session.is_expired_at(NOW)
    assert not session.is_valid_at(session.expires_at)
    assert session.is_expired_at(session.expires_at)

    revoked = make_session(revoked_at=NOW + timedelta(minutes=1))
    assert not revoked.is_valid_at(NOW)

    with pytest.raises(ValueError, match="token_hash"):
        make_session(token_hash="raw-session-token")


def test_session_rejects_naive_or_non_future_expiration():
    with pytest.raises(ValueError, match="timezone"):
        make_session(expires_at=datetime(2026, 1, 2, 4, 4))

    with pytest.raises(ValueError, match="later"):
        make_session(expires_at=NOW)
