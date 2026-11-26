import json
from hashlib import sha256
from http.cookies import SimpleCookie
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from app.auth.dependencies import SESSION_COOKIE_NAME
from app.auth.models import UserRole
from app.auth.service import InvalidSessionError
from app.auth.sqlite_repository import SQLiteAuthRepository
from app.incidents.sqlite_repository import SQLiteIncidentRepository
from app.main import create_app
from app.schema_registry.sqlite_repository import SQLiteSchemaRegistryRepository


@pytest.fixture
def auth_api(tmp_path, monkeypatch):
    database_path = tmp_path / "auth-api.sqlite3"
    monkeypatch.setenv("VIGIA_DATABASE_PATH", str(database_path))
    monkeypatch.delenv("VIGIA_COOKIE_SECURE", raising=False)
    monkeypatch.delenv("VIGIA_ENV", raising=False)

    app = create_app()
    active_user = app.state.auth_service.create_user(
        "victor",
        "correct-password",
        role=UserRole.ANALYST,
    )
    inactive_user = app.state.auth_service.create_user(
        "inactive",
        "correct-password",
        role=UserRole.ADMIN,
        is_active=False,
    )

    with TestClient(app, base_url="https://testserver") as client:
        yield app, client, active_user, inactive_user


def _login(
    client: TestClient,
    login: str = "victor",
    password: str = "correct-password",
):
    return client.post(
        "/auth/login",
        json={"login": login, "password": password},
    )


def _response_cookie(response, name: str):
    cookie = SimpleCookie()
    cookie.load(response.headers["set-cookie"])
    return cookie[name]


def test_login_sets_secure_session_cookie_and_returns_no_secrets(auth_api):
    app, client, user, _ = auth_api

    response = _login(client)

    assert response.status_code == 200
    assert response.json() == {
        "user_id": str(user.user_id),
        "role": UserRole.ANALYST.value,
    }

    session_cookie = _response_cookie(response, SESSION_COOKIE_NAME)
    assert session_cookie["httponly"]
    assert session_cookie["secure"]
    assert session_cookie["samesite"].lower() == "lax"
    assert session_cookie["path"] == "/"
    assert int(session_cookie["max-age"]) > 0

    token = session_cookie.value
    token_hash = sha256(token.encode("utf-8")).hexdigest()
    assert token not in response.text
    assert token_hash not in response.text
    assert app.state.auth_repository.get_user(user.user_id).password_hash not in (
        response.text
    )
    assert app.state.auth_repository.get_session_by_token_hash(token_hash) is not None


def test_login_invalid_credentials_and_inactive_user_return_same_401(auth_api):
    _, client, _, _ = auth_api

    missing_user_response = _login(client, login="unknown", password="wrong-password")
    wrong_password_response = _login(client, password="wrong-password")
    inactive_user_response = _login(
        client,
        login="inactive",
        password="correct-password",
    )

    assert missing_user_response.status_code == 401
    assert wrong_password_response.status_code == 401
    assert inactive_user_response.status_code == 401
    assert missing_user_response.json() == wrong_password_response.json()
    assert missing_user_response.json() == inactive_user_response.json()


def test_login_rejects_client_supplied_role(auth_api):
    _, client, _, _ = auth_api

    response = client.post(
        "/auth/login",
        json={
            "login": "victor",
            "password": "correct-password",
            "role": UserRole.ADMIN.value,
        },
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    ("login", "password"),
    [
        ("l" * 255, "valid-password"),
        ("victor", ""),
        ("victor", "p" * 1025),
    ],
    ids=["login-too-long", "password-empty", "password-too-long"],
)
def test_login_rejects_credentials_outside_schema_limits(auth_api, login, password):
    _, client, _, _ = auth_api

    response = _login(client, login=login, password=password)

    assert response.status_code == 422


def test_oversized_password_is_not_reflected_in_validation_error(auth_api):
    _, client, _, _ = auth_api
    password = "oversized-secret-" + ("x" * 1024)

    response = client.post(
        "/auth/login",
        json={"login": "victor", "password": password},
    )

    assert response.status_code == 422
    response_json = response.json()
    assert password not in json.dumps(response_json)
    password_error = next(
        error
        for error in response_json["detail"]
        if error["loc"] == ["body", "password"]
    )
    assert password_error["type"] == "string_too_long"
    assert password_error["msg"]


@pytest.mark.parametrize(
    "payload",
    [
        {"password": "secret-in-missing-login"},
        {"login": "victor", "password": {"value": "secret-in-invalid-type"}},
    ],
    ids=["missing-login", "invalid-password-type"],
)
def test_login_validation_does_not_reflect_sensitive_input(auth_api, payload):
    _, client, _, _ = auth_api

    response = client.post("/auth/login", json=payload)

    assert response.status_code == 422
    response_json = response.json()
    for submitted_value in ("secret-in-missing-login", "secret-in-invalid-type"):
        assert submitted_value not in json.dumps(response_json)
    assert response_json["detail"]
    assert all(
        {"loc", "type", "msg"} <= error.keys()
        for error in response_json["detail"]
    )


def test_me_requires_and_returns_a_valid_session(auth_api):
    _, client, user, _ = auth_api

    missing_session_response = client.get("/auth/me")
    login_response = _login(client)
    me_response = client.get("/auth/me")

    assert missing_session_response.status_code == 401
    assert login_response.status_code == 200
    assert me_response.status_code == 200
    assert me_response.json() == {
        "user_id": str(user.user_id),
        "role": UserRole.ANALYST.value,
    }


def test_logout_revokes_the_current_session_and_clears_cookie(auth_api):
    app, client, _, _ = auth_api
    login_response = _login(client)
    assert login_response.status_code == 200
    session_token = client.cookies.get(SESSION_COOKIE_NAME)
    assert session_token

    logout_response = client.post("/auth/logout")

    assert logout_response.status_code == 204
    cleared_cookie = _response_cookie(logout_response, SESSION_COOKIE_NAME)
    assert cleared_cookie.value == ""
    assert cleared_cookie["max-age"] == "0"
    assert cleared_cookie["httponly"]
    assert cleared_cookie["secure"]
    assert cleared_cookie["samesite"].lower() == "lax"
    assert cleared_cookie["path"] == "/"
    with pytest.raises(InvalidSessionError):
        app.state.auth_service.validate_session(session_token)
    assert client.get("/auth/me").status_code == 401


def test_logout_clears_cookie_when_revocation_raises(auth_api, monkeypatch):
    app, client, _, _ = auth_api
    login_response = _login(client)
    assert login_response.status_code == 200

    def fail_logout(session_token: str) -> bool:
        _ = session_token
        raise RuntimeError("Internal repository failure.")

    monkeypatch.setattr(app.state.auth_service, "logout", fail_logout)

    response = client.post("/auth/logout")

    assert response.status_code == 500
    assert response.json() == {
        "detail": "An unexpected authentication error occurred."
    }
    cleared_cookie = _response_cookie(response, SESSION_COOKIE_NAME)
    assert cleared_cookie.value == ""
    assert cleared_cookie["max-age"] == "0"
    assert cleared_cookie["httponly"]
    assert cleared_cookie["secure"]
    assert cleared_cookie["samesite"].lower() == "lax"
    assert cleared_cookie["path"] == "/"


@pytest.mark.parametrize(
    "cookie_value",
    [None, "invalid-session-token"],
    ids=["absent-cookie", "invalid-cookie"],
)
def test_logout_without_valid_cookie_is_idempotent(auth_api, cookie_value):
    _, client, _, _ = auth_api
    cookies = (
        {}
        if cookie_value is None
        else {SESSION_COOKIE_NAME: cookie_value}
    )

    response = client.post("/auth/logout", cookies=cookies)

    assert response.status_code == 204
    cleared_cookie = _response_cookie(response, SESSION_COOKIE_NAME)
    assert cleared_cookie.value == ""
    assert cleared_cookie["max-age"] == "0"
    assert cleared_cookie["httponly"]
    assert cleared_cookie["secure"]
    assert cleared_cookie["samesite"].lower() == "lax"
    assert cleared_cookie["path"] == "/"


def test_default_sqlite_auth_repository_uses_business_database_path(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "configured" / "vigia.sqlite3"
    monkeypatch.setenv("VIGIA_DATABASE_PATH", str(database_path))
    monkeypatch.delenv("VIGIA_COOKIE_SECURE", raising=False)
    monkeypatch.delenv("VIGIA_ENV", raising=False)

    app = create_app()

    assert isinstance(app.state.auth_repository, SQLiteAuthRepository)
    assert (
        Path(app.state.auth_repository.database_path).resolve()
        == database_path.resolve()
    )
    assert (
        Path(app.state.dataset_analysis_repository.database_path).resolve()
        == database_path.resolve()
    )


def test_custom_business_repositories_require_auth_repository(tmp_path):
    database_path = tmp_path / "custom.sqlite3"
    schema_repository = SQLiteSchemaRegistryRepository(database_path)
    incident_repository = SQLiteIncidentRepository(database_path)

    with pytest.raises(ValueError, match="auth_repository is required"):
        create_app(
            repository=schema_repository,
            incident_repository=incident_repository,
        )


def test_injected_sqlite_auth_repository_must_use_same_database_path(tmp_path):
    database_path = tmp_path / "business.sqlite3"
    schema_repository = SQLiteSchemaRegistryRepository(database_path)
    incident_repository = SQLiteIncidentRepository(database_path)
    auth_repository = SQLiteAuthRepository(tmp_path / "auth.sqlite3")

    with pytest.raises(ValueError, match="same database path"):
        create_app(
            repository=schema_repository,
            incident_repository=incident_repository,
            auth_repository=auth_repository,
        )


def test_injected_sqlite_auth_repository_must_use_same_busy_timeout(tmp_path):
    database_path = tmp_path / "shared.sqlite3"
    schema_repository = SQLiteSchemaRegistryRepository(database_path)
    incident_repository = SQLiteIncidentRepository(database_path)
    auth_repository = SQLiteAuthRepository(database_path, busy_timeout_ms=1000)

    with pytest.raises(ValueError, match="same busy timeout"):
        create_app(
            repository=schema_repository,
            incident_repository=incident_repository,
            auth_repository=auth_repository,
        )


def test_matching_sqlite_auth_repository_is_injected(tmp_path):
    database_path = tmp_path / "shared.sqlite3"
    schema_repository = SQLiteSchemaRegistryRepository(
        database_path,
        busy_timeout_ms=2500,
    )
    incident_repository = SQLiteIncidentRepository(
        database_path,
        busy_timeout_ms=2500,
    )
    auth_repository = SQLiteAuthRepository(
        database_path,
        busy_timeout_ms=2500,
    )

    app = create_app(
        repository=schema_repository,
        incident_repository=incident_repository,
        auth_repository=auth_repository,
    )

    assert app.state.auth_repository is auth_repository


def test_secure_cookie_can_be_disabled_only_for_explicit_development(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("VIGIA_DATABASE_PATH", str(tmp_path / "development.sqlite3"))
    monkeypatch.setenv("VIGIA_ENV", "development")
    monkeypatch.setenv("VIGIA_COOKIE_SECURE", "false")

    development_app = create_app()
    development_user = development_app.state.auth_service.create_user(
        "local-user",
        "local-password",
    )
    with TestClient(development_app, base_url="http://testserver") as client:
        login_response = client.post(
            "/auth/login",
            json={
                "login": development_user.login,
                "password": "local-password",
            },
        )

    assert development_app.state.auth_cookie_secure is False
    assert login_response.status_code == 200
    assert not _response_cookie(login_response, SESSION_COOKIE_NAME)["secure"]

    monkeypatch.setenv("VIGIA_ENV", "production")
    with pytest.raises(ValueError, match="only when VIGIA_ENV=development"):
        create_app()
