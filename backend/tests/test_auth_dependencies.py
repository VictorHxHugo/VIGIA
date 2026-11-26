from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
import pytest

from app.auth.dependencies import (
    Permission,
    ROLE_PERMISSIONS,
    SESSION_COOKIE_NAME,
    get_current_principal,
    require_permission,
)
from app.auth.models import (
    Principal,
    Session,
    User,
    UserRole,
    normalize_login,
)
from app.auth.service import AuthService


class StubPasswordHasher:
    def hash(self, password: str) -> str:
        _ = password
        return "stub-hash"

    def verify(self, password: str, hash: str) -> bool:
        _ = (password, hash)
        return False


class StubAuthRepository:
    def __init__(self, user: User, session: Session) -> None:
        self.users = {user.user_id: user}
        self.sessions = {session.token_hash: session}

    def create_user(self, user: User) -> User:
        self.users[user.user_id] = user
        return user

    def get_user(self, user_id: UUID) -> User | None:
        return self.users.get(user_id)

    def get_user_by_normalized_login(self, normalized_login: str) -> User | None:
        normalized = normalize_login(normalized_login)
        return next(
            (
                user
                for user in self.users.values()
                if user.normalized_login == normalized
            ),
            None,
        )

    def create_session(self, session: Session) -> Session:
        self.sessions[session.token_hash] = session
        return session

    def get_session_by_token_hash(self, token_hash: str) -> Session | None:
        return self.sessions.get(token_hash)

    def revoke_session(self, token_hash: str, revoked_at: datetime) -> bool:
        session = self.sessions.get(token_hash)
        if session is None or session.revoked_at is not None:
            return False
        self.sessions[token_hash] = replace(session, revoked_at=revoked_at)
        return True

    def revoke_user_sessions(self, user_id: UUID, revoked_at: datetime) -> int:
        revoked_count = 0
        for token_hash, session in tuple(self.sessions.items()):
            if session.user_id == user_id and session.revoked_at is None:
                self.sessions[token_hash] = replace(
                    session,
                    revoked_at=revoked_at,
                )
                revoked_count += 1
        return revoked_count


def make_client(
    role: UserRole = UserRole.VIEWER,
    *,
    session_state: str = "valid",
    dependency: Callable[..., Principal] = get_current_principal,
) -> tuple[TestClient, str, User]:
    now = datetime.now(timezone.utc)
    session_token = f"test-session-{uuid4()}"
    user = User(
        user_id=uuid4(),
        login="victor",
        password_hash="unused-stub-hash",
        role=role,
    )
    created_at = now - timedelta(minutes=2)
    expires_at = now + timedelta(hours=1)
    revoked_at = None
    if session_state == "expired":
        expires_at = now - timedelta(minutes=1)
    elif session_state == "revoked":
        revoked_at = now - timedelta(minutes=1)

    session = Session(
        token_hash=sha256(session_token.encode("utf-8")).hexdigest(),
        user_id=user.user_id,
        created_at=created_at,
        expires_at=expires_at,
        revoked_at=revoked_at,
    )
    repository = StubAuthRepository(user, session)
    auth_service = AuthService(
        repository,
        password_hasher=StubPasswordHasher(),
    )
    app = FastAPI()
    app.state.auth_service = auth_service

    @app.get("/protected")
    def protected(
        principal: Principal = Depends(dependency),
    ) -> dict[str, str]:
        return {
            "user_id": str(principal.user_id),
            "role": principal.role.value,
        }

    return TestClient(app), session_token, user


def test_missing_cookie_returns_401():
    client, _, _ = make_client()

    response = client.get("/protected")

    assert response.status_code == 401


def test_invalid_cookie_returns_401():
    client, _, _ = make_client()

    response = client.get(
        "/protected",
        cookies={SESSION_COOKIE_NAME: "not-a-valid-session"},
    )

    assert response.status_code == 401


@pytest.mark.parametrize("session_state", ["expired", "revoked"])
def test_expired_or_revoked_cookie_returns_401(session_state: str):
    client, session_token, _ = make_client(session_state=session_state)

    response = client.get(
        "/protected",
        cookies={SESSION_COOKIE_NAME: session_token},
    )

    assert response.status_code == 401


def test_authenticated_cookie_returns_the_principal():
    client, session_token, user = make_client(UserRole.ANALYST)

    response = client.get(
        "/protected",
        cookies={SESSION_COOKIE_NAME: session_token},
    )

    assert response.status_code == 200
    assert response.json() == {
        "user_id": str(user.user_id),
        "role": UserRole.ANALYST.value,
    }


def test_role_permission_mapping_is_server_defined_and_complete():
    read_permissions = {
        Permission.INCIDENT_READ,
        Permission.SCHEMA_READ,
    }
    analyst_permissions = read_permissions | {
        Permission.DATASET_ANALYZE,
    }
    operator_permissions = analyst_permissions | {
        Permission.INCIDENT_STATUS_CHANGE,
    }

    assert ROLE_PERMISSIONS[UserRole.VIEWER] == read_permissions
    assert ROLE_PERMISSIONS[UserRole.ANALYST] == analyst_permissions
    assert ROLE_PERMISSIONS[UserRole.INCIDENT_OPERATOR] == operator_permissions
    assert ROLE_PERMISSIONS[UserRole.ADMIN] == set(Permission)


def test_permission_dependency_allows_authorized_principal():
    client, session_token, user = make_client(
        UserRole.ANALYST,
        dependency=require_permission(Permission.DATASET_ANALYZE),
    )

    response = client.get(
        "/protected",
        cookies={SESSION_COOKIE_NAME: session_token},
    )

    assert response.status_code == 200
    assert response.json()["user_id"] == str(user.user_id)


def test_authenticated_user_without_permission_gets_403():
    client, session_token, _ = make_client(
        UserRole.VIEWER,
        dependency=require_permission(Permission.SCHEMA_APPROVE),
    )

    response = client.get(
        "/protected",
        cookies={SESSION_COOKIE_NAME: session_token},
    )

    assert response.status_code == 403


def test_client_supplied_role_cannot_grant_permission():
    client, session_token, _ = make_client(
        UserRole.VIEWER,
        dependency=require_permission(Permission.SCHEMA_APPROVE),
    )

    response = client.get(
        "/protected?role=admin",
        cookies={SESSION_COOKIE_NAME: session_token},
    )

    assert response.status_code == 403


def test_unknown_permission_is_rejected_when_dependency_is_created():
    with pytest.raises(ValueError, match="Unknown permission"):
        require_permission("user:delete")
