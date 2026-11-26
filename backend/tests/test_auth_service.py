from dataclasses import replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from uuid import UUID, uuid4

import pytest
from pwdlib import PasswordHash

import app.auth.service as auth_service_module
from app.auth.models import Principal, Session, User, UserRole, normalize_login
from app.auth.repository import (
    SessionAlreadyExistsError,
    UserAlreadyExistsError,
    UserNotFoundError,
)
from app.auth.service import (
    AuthService,
    InvalidCredentialsError,
    InvalidSessionError,
)


def _encoded_test_password(password: str) -> str:
    return "test$" + sha256(password.encode("utf-8")).hexdigest()


class FakePasswordHasher:
    def __init__(self) -> None:
        self.hash_calls: list[str] = []
        self.verify_calls: list[tuple[str, str]] = []

    def hash(self, password: str) -> str:
        hashed_password = _encoded_test_password(password)
        self.hash_calls.append(hashed_password)
        return hashed_password

    def verify(self, password: str, hash: str) -> bool:
        self.verify_calls.append((password, hash))
        return hash == _encoded_test_password(password)


class FakeAuthRepository:
    def __init__(self) -> None:
        self.users: dict[UUID, User] = {}
        self.sessions: dict[str, Session] = {}
        self.revoke_user_calls: list[tuple[UUID, datetime]] = []

    def create_user(self, user: User) -> User:
        if any(
            existing.normalized_login == user.normalized_login
            for existing in self.users.values()
        ):
            raise UserAlreadyExistsError(user.normalized_login)
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
        if session.user_id not in self.users:
            raise UserNotFoundError(session.user_id)
        if session.token_hash in self.sessions:
            raise SessionAlreadyExistsError()
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
        self.revoke_user_calls.append((user_id, revoked_at))
        revoked_count = 0
        for token_hash, session in tuple(self.sessions.items()):
            if session.user_id == user_id and session.revoked_at is None:
                self.sessions[token_hash] = replace(
                    session,
                    revoked_at=revoked_at,
                )
                revoked_count += 1
        return revoked_count


def make_service(
    *,
    session_lifetime: timedelta = timedelta(hours=8),
) -> tuple[FakeAuthRepository, AuthService, FakePasswordHasher]:
    repository = FakeAuthRepository()
    password_hasher = FakePasswordHasher()
    service = AuthService(
        repository,
        session_lifetime=session_lifetime,
        password_hasher=password_hasher,
    )
    return repository, service, password_hasher


def test_hashes_new_passwords_with_argon2id_and_verifies_them():
    repository = FakeAuthRepository()
    password_hasher = PasswordHash.recommended()
    service = AuthService(repository, password_hasher=password_hasher)

    user = service.create_user("victor", "correct horse battery staple")

    assert user.password_hash.startswith("$argon2id$")
    assert password_hasher.verify(
        "correct horse battery staple",
        user.password_hash,
    )
    assert "correct horse battery staple" not in user.password_hash


def test_incorrect_password_does_not_authenticate():
    repository, service, _ = make_service()
    service.create_user("victor", "correct password")

    with pytest.raises(InvalidCredentialsError):
        service.authenticate("victor", "incorrect password")

    assert repository.sessions == {}


def test_missing_user_wrong_password_and_inactive_user_share_public_error():
    repository, service, _ = make_service()
    user = service.create_user("victor", "correct password")
    repository.users[user.user_id] = replace(user, is_active=False)

    failures = []
    for login, password in (
        ("unknown", "correct password"),
        ("victor", "incorrect password"),
        ("victor", "correct password"),
    ):
        with pytest.raises(InvalidCredentialsError) as error:
            service.authenticate(login, password)
        failures.append((type(error.value), str(error.value)))

    assert len(set(failures)) == 1


def test_missing_login_uses_the_dummy_password_hash():
    _, service, password_hasher = make_service()
    dummy_hash = password_hasher.hash_calls[0]

    with pytest.raises(InvalidCredentialsError):
        service.authenticate("not-registered", "candidate password")

    assert password_hasher.verify_calls == [
        ("candidate password", dummy_hash)
    ]


def test_issued_tokens_are_random_and_only_the_sha256_hash_is_stored(monkeypatch):
    repository, service, _ = make_service()
    service.create_user("victor", "correct password")
    requested_token_sizes = []
    original_token_urlsafe = auth_service_module.secrets.token_urlsafe

    def record_token_size(nbytes: int | None = None) -> str:
        requested_token_sizes.append(nbytes)
        return original_token_urlsafe(nbytes)

    monkeypatch.setattr(
        auth_service_module.secrets,
        "token_urlsafe",
        record_token_size,
    )

    first = service.authenticate("victor", "correct password")
    second = service.authenticate("victor", "correct password")

    assert requested_token_sizes == [32, 32]
    assert first.session_token != second.session_token
    assert first.session_token not in repr(first)
    assert second.session_token not in repr(second)

    for result in (first, second):
        expected_hash = sha256(result.session_token.encode("utf-8")).hexdigest()
        assert expected_hash in repository.sessions
        assert repository.sessions[expected_hash].token_hash == expected_hash
        assert result.session_token not in repository.sessions


def test_session_expiry_uses_configured_absolute_lifetime_and_utc():
    repository, service, _ = make_service(
        session_lifetime=timedelta(minutes=30),
    )
    service.create_user("victor", "correct password")

    result = service.authenticate("victor", "correct password")
    session = repository.sessions[
        sha256(result.session_token.encode("utf-8")).hexdigest()
    ]

    assert session.expires_at - session.created_at == timedelta(minutes=30)
    assert session.created_at.tzinfo is timezone.utc
    assert session.expires_at.tzinfo is timezone.utc
    assert result.expires_at == session.expires_at


def test_valid_session_returns_the_current_principal():
    repository, service, _ = make_service()
    user = service.create_user(
        "victor",
        "correct password",
        role=UserRole.ANALYST,
    )
    result = service.authenticate("victor", "correct password")

    assert service.validate_session(result.session_token) == Principal(
        user_id=user.user_id,
        role=UserRole.ANALYST,
    )
    assert repository.sessions


def test_missing_session_is_rejected():
    _, service, _ = make_service()

    with pytest.raises(InvalidSessionError):
        service.validate_session("unknown-session-token")


def test_expired_session_is_rejected():
    repository, service, _ = make_service()
    user = service.create_user("victor", "correct password")
    now = datetime.now(timezone.utc)
    expired_session = Session(
        token_hash=sha256(b"expired-token").hexdigest(),
        user_id=user.user_id,
        created_at=now - timedelta(hours=2),
        expires_at=now - timedelta(hours=1),
    )
    repository.create_session(expired_session)

    with pytest.raises(InvalidSessionError):
        service.validate_session("expired-token")


def test_revoked_session_is_rejected():
    _, service, _ = make_service()
    service.create_user("victor", "correct password")
    result = service.authenticate("victor", "correct password")

    assert service.logout(result.session_token)
    with pytest.raises(InvalidSessionError):
        service.validate_session(result.session_token)


def test_deactivating_a_user_invalidates_existing_sessions():
    repository, service, _ = make_service()
    user = service.create_user("victor", "correct password")
    result = service.authenticate("victor", "correct password")
    repository.users[user.user_id] = replace(user, is_active=False)

    with pytest.raises(InvalidSessionError):
        service.validate_session(result.session_token)


def test_logout_is_idempotent_and_revokes_the_session():
    repository, service, _ = make_service()
    service.create_user("victor", "correct password")
    result = service.authenticate("victor", "correct password")

    assert service.logout(result.session_token) is True
    assert service.logout(result.session_token) is False
    assert next(iter(repository.sessions.values())).revoked_at is not None


def test_revoking_user_sessions_delegates_to_repository():
    repository, service, _ = make_service()
    first_user = service.create_user("victor", "correct password")
    second_user = service.create_user("other", "other password")
    service.authenticate("victor", "correct password")
    service.authenticate("victor", "correct password")
    service.authenticate("other", "other password")

    assert service.revoke_user_sessions(first_user.user_id) == 2
    assert len(repository.revoke_user_calls) == 1
    delegated_user_id, revoked_at = repository.revoke_user_calls[0]
    assert delegated_user_id == first_user.user_id
    assert revoked_at.tzinfo is timezone.utc
    assert sum(
        session.revoked_at is not None
        for session in repository.sessions.values()
        if session.user_id == second_user.user_id
    ) == 0


def test_repository_errors_are_not_hidden():
    _, service, _ = make_service()
    service.create_user("victor", "correct password")

    with pytest.raises(UserAlreadyExistsError):
        service.create_user(" VICTOR ", "another password")

    class FailingRepository(FakeAuthRepository):
        def get_user_by_normalized_login(
            self,
            normalized_login: str,
        ) -> User | None:
            _ = normalized_login
            raise RuntimeError("repository unavailable")

    failing_service = AuthService(
        FailingRepository(),
        password_hasher=FakePasswordHasher(),
    )
    with pytest.raises(RuntimeError, match="repository unavailable"):
        failing_service.authenticate("victor", "correct password")


def test_missing_user_during_session_creation_uses_generic_credentials_error():
    class DeletingRepository(FakeAuthRepository):
        def create_session(self, session: Session) -> Session:
            raise UserNotFoundError(session.user_id)

    repository = DeletingRepository()
    password_hasher = FakePasswordHasher()
    service = AuthService(repository, password_hasher=password_hasher)
    user = User(
        user_id=uuid4(),
        login="victor",
        password_hash=password_hasher.hash("correct password"),
        role=UserRole.ANALYST,
    )
    repository.users[user.user_id] = user

    with pytest.raises(InvalidCredentialsError) as error:
        service.authenticate("victor", "correct password")

    assert str(error.value) == "Invalid login or password."


def test_non_positive_session_lifetime_is_rejected():
    with pytest.raises(ValueError, match="positive"):
        AuthService(
            FakeAuthRepository(),
            session_lifetime=timedelta(0),
            password_hasher=FakePasswordHasher(),
        )
