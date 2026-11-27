from datetime import datetime, timedelta, timezone
from threading import Event, Lock
from typing import cast

import anyio
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
import pytest

from app import main as main_module
from app.auth.dependencies import SESSION_COOKIE_NAME
from app.auth.models import UserRole
from app.auth.sqlite_repository import SQLiteAuthRepository
from app.core.analysis_limits import MAX_ANALYSIS_COLUMNS
from app.datasets.sqlite_repository import SQLiteDatasetAnalysisRepository
from app.incidents.models import (
    Incident,
    IncidentOccurrence,
    IncidentSeverity,
    IncidentStatus,
    IncidentStatusChange,
)
from app.incidents.repository import IncidentFilters, IncidentRepository
from app.incidents.sqlite_repository import (
    SQLiteIncidentRepository,
    SQLiteIncidentUnitOfWork,
)
from app.main import MAX_UPLOAD_SIZE, create_app
from app.schema_registry.repository import SchemaRegistryRepository
from app.schema_registry.sqlite_repository import SQLiteSchemaRegistryRepository


@pytest.fixture
def api_client(tmp_path):
    database_path = tmp_path / "registry.sqlite3"
    repository = SQLiteSchemaRegistryRepository(database_path)
    incident_repository = SQLiteIncidentRepository(database_path)
    app = create_app(
        repository=repository,
        incident_repository=incident_repository,
        auth_repository=SQLiteAuthRepository(database_path),
    )
    app.state.auth_service.create_user(
        "api-admin",
        "api-admin-password",
        role=UserRole.ADMIN,
    )

    with TestClient(app, base_url="https://testserver") as client:
        authenticated_session = app.state.auth_service.authenticate(
            "api-admin",
            "api-admin-password",
        )
        client.cookies.set(
            SESSION_COOKIE_NAME,
            authenticated_session.session_token,
        )
        yield client, repository


@pytest.fixture
def authorization_api(tmp_path):
    database_path = tmp_path / "authorization.sqlite3"
    repository = SQLiteSchemaRegistryRepository(database_path)
    incident_repository = SQLiteIncidentRepository(database_path)
    app = create_app(
        repository=repository,
        incident_repository=incident_repository,
        auth_repository=SQLiteAuthRepository(database_path),
    )

    passwords = {}
    for role in (
        UserRole.VIEWER,
        UserRole.ANALYST,
        UserRole.INCIDENT_OPERATOR,
        UserRole.ADMIN,
    ):
        login = f"api-{role.value}"
        password = f"{role.value}-password"
        app.state.auth_service.create_user(login, password, role=role)
        passwords[role] = (login, password)

    with TestClient(app, base_url="https://testserver") as client:
        yield app, client, repository, incident_repository, passwords


def authenticate_api_role(app, client, role, passwords):
    client.cookies.clear()
    login, password = passwords[role]
    authenticated_session = app.state.auth_service.authenticate(login, password)
    client.cookies.set(
        SESSION_COOKIE_NAME,
        authenticated_session.session_token,
    )
    return authenticated_session


def authenticate_admin(app, client, login="api-admin"):
    password = f"{login}-password"
    app.state.auth_service.create_user(
        login,
        password,
        role=UserRole.ADMIN,
    )
    authenticated_session = app.state.auth_service.authenticate(
        login,
        password,
    )
    client.cookies.clear()
    client.cookies.set(
        SESSION_COOKIE_NAME,
        authenticated_session.session_token,
    )
    return authenticated_session


def upload_csv(
    client: TestClient,
    dataset_id: str,
    content: bytes,
    *,
    idempotency_key: str,
    filename: str = "customers.csv",
):
    return client.post(
        "/datasets/analyze",
        data={"dataset_id": dataset_id},
        files={"file": (filename, content, "text/csv")},
        headers={"Idempotency-Key": idempotency_key},
    )


async def upload_csv_async(
    client: AsyncClient,
    dataset_id: str,
    content: bytes,
    *,
    idempotency_key: str,
):
    return await client.post(
        "/datasets/analyze",
        data={"dataset_id": dataset_id},
        files={"file": ("customers.csv", content, "text/csv")},
        headers={"Idempotency-Key": idempotency_key},
    )


def block_first_analysis(monkeypatch, started: Event, release: Event) -> None:
    original_process = main_module._process_dataset_analysis
    lock = Lock()
    should_block = True

    def blocked_process(*args, **kwargs):
        nonlocal should_block
        with lock:
            block_this_call = should_block
            should_block = False

        if block_this_call:
            started.set()
            if not release.wait(timeout=10):
                raise TimeoutError("Test did not release the blocked analysis.")

        return original_process(*args, **kwargs)

    monkeypatch.setattr(
        main_module,
        "_process_dataset_analysis",
        blocked_process,
    )


API_TEST_TIME = datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc)


def make_api_incident(
    incident_id: str,
    *,
    dataset_id: str = "customers",
    status: IncidentStatus = IncidentStatus.OPEN,
    severity: IncidentSeverity = IncidentSeverity.HIGH,
    code: str = "NULL_VALUES",
    updated_at: datetime = API_TEST_TIME,
    revision: int = 1,
    resolved_at: datetime | None = None,
) -> Incident:
    return Incident(
        incident_id=incident_id,
        dataset_id=dataset_id,
        fingerprint=f"fingerprint-{incident_id}",
        code=code,
        title="Null values found",
        description="A required field contains null values.",
        column="email",
        severity=severity,
        status=status,
        created_at=API_TEST_TIME,
        updated_at=updated_at,
        first_seen_at=API_TEST_TIME,
        last_seen_at=updated_at,
        resolved_at=resolved_at,
        occurrence_count=1,
        revision=revision,
    )


def make_api_occurrence(
    occurrence_id: str,
    incident_id: str,
    detected_at: datetime,
) -> IncidentOccurrence:
    return IncidentOccurrence(
        occurrence_id=occurrence_id,
        incident_id=incident_id,
        analysis_id=f"analysis-{occurrence_id}",
        detected_at=detected_at,
        code="NULL_VALUES",
        title="Null values found",
        description="A required field contains null values.",
        column="email",
        severity=IncidentSeverity.HIGH,
        affected_rows=2,
        examples=(1, 3),
    )


def make_api_status_change(
    change_id: str,
    incident_id: str,
    from_status: IncidentStatus | None,
    to_status: IncidentStatus,
    changed_at: datetime,
) -> IncidentStatusChange:
    return IncidentStatusChange(
        change_id=change_id,
        incident_id=incident_id,
        from_status=from_status,
        to_status=to_status,
        changed_at=changed_at,
        actor="seed",
        reason="Seeded test history.",
    )


def store_api_incident(
    repository: SQLiteIncidentRepository,
    incident: Incident,
    *,
    occurrences: tuple[IncidentOccurrence, ...] = (),
    history: tuple[IncidentStatusChange, ...] = (),
) -> None:
    with repository.transaction() as unit_of_work:
        unit_of_work.create_incident(incident)
        for occurrence in occurrences:
            unit_of_work.record_occurrence(occurrence)
        for change in history:
            unit_of_work.record_status_change(change)


def test_root_endpoint(api_client):
    client, _ = api_client

    response = client.get("/")

    assert response.status_code == 200
    assert response.json()["name"] == "VIGIA"


def test_health_endpoint(api_client):
    client, _ = api_client

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


def test_analyze_valid_csv_creates_baseline(api_client):
    client, repository = api_client
    content = b"id,name\n1,Ana\n2,Bruno\n3,Carlos"

    response = upload_csv(
        client,
        "customers",
        content,
        idempotency_key="valid-analysis",
    )

    assert response.status_code == 200
    result = response.json()

    assert result["dataset_id"] == "customers"
    assert result["baseline_version"] == 1
    assert result["baseline_created"] is True
    assert result["schema_drift_issues"] == []
    assert result["quality_report"]["dataset_name"] == "customers.csv"
    assert result["quality_report"]["total_rows"] == 3
    assert result["quality_report"]["total_columns"] == 2
    assert result["quality_report"]["quality_score"] == 100.0
    assert result["incident_processing"] == {
        "created_count": 0,
        "updated_count": 0,
        "reopened_count": 0,
        "ignored_occurrence_count": 0,
    }
    baseline = repository.get_current("customers")
    assert baseline is not None
    assert baseline.version == 1


def test_second_upload_with_identical_schema_has_no_drift(api_client):
    client, _ = api_client
    first = upload_csv(
        client,
        "customers",
        b"id,name\n1,Ana",
        idempotency_key="identical-schema-first",
    )
    second = upload_csv(
        client,
        "customers",
        b"id,name\n2,Bruno",
        idempotency_key="identical-schema-second",
        filename="customers-latest.csv",
    )

    assert first.status_code == 200
    assert second.status_code == 200
    result = second.json()
    assert result["baseline_version"] == 1
    assert result["baseline_created"] is False
    assert result["schema_drift_issues"] == []
    assert result["quality_report"]["dataset_name"] == "customers-latest.csv"


def test_upload_with_schema_drift_preserves_persisted_baseline(api_client, tmp_path):
    client, repository = api_client
    upload_csv(
        client,
        "customers",
        b"id,name\n1,Ana",
        idempotency_key="drift-baseline",
    )

    response = upload_csv(
        client,
        "customers",
        b"id,name,email\n2,Bruno,bruno@example.com",
        idempotency_key="drift-detection",
    )

    assert response.status_code == 200
    result = response.json()
    assert [issue["code"] for issue in result["schema_drift_issues"]] == [
        "COLUMN_ADDED"
    ]
    assert result["baseline_version"] == 1
    assert result["baseline_created"] is False
    assert result["incident_processing"]["created_count"] == 1

    baseline = repository.get_current("customers")
    assert baseline is not None
    assert baseline.version == 1
    assert baseline.schema == {"id": "int64", "name": "str"}
    incident_repository = SQLiteIncidentRepository(tmp_path / "registry.sqlite3")
    incidents = incident_repository.list_incidents(
        IncidentFilters(dataset_id="customers")
    )
    assert len(incidents) == 1
    occurrences = incident_repository.list_occurrences(incidents[0].incident_id)
    assert [occurrence.analysis_id for occurrence in occurrences] == [
        "drift-detection"
    ]


def test_analyze_requires_idempotency_key(api_client):
    client, _ = api_client

    response = client.post(
        "/datasets/analyze",
        data={"dataset_id": "customers"},
        files={"file": ("customers.csv", b"id\n1", "text/csv")},
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    "idempotency_key",
    ["", "   ", " analysis-123", "analysis-123 "],
)
def test_analyze_rejects_invalid_idempotency_key(
    api_client,
    tmp_path,
    idempotency_key,
):
    client, repository = api_client

    response = upload_csv(
        client,
        "customers",
        b"id\n1",
        idempotency_key=idempotency_key,
    )

    assert response.status_code == 422
    assert "Idempotency-Key" in response.json()["detail"]
    assert repository.get_current("customers") is None
    incident_repository = SQLiteIncidentRepository(
        tmp_path / "registry.sqlite3"
    )
    assert incident_repository.list_incidents(
        IncidentFilters(dataset_id="customers")
    ) == []


def test_retry_with_same_idempotency_key_does_not_duplicate_occurrence(
    api_client,
    tmp_path,
):
    client, _ = api_client
    upload_csv(
        client,
        "customers",
        b"id,name\n1,Ana",
        idempotency_key="retry-baseline",
    )
    first = upload_csv(
        client,
        "customers",
        b"id,name,email\n2,Bruno,bruno@example.com",
        idempotency_key="same-analysis-key",
    )
    retry = upload_csv(
        client,
        "customers",
        b"id,name,email\n2,Bruno,bruno@example.com",
        idempotency_key="same-analysis-key",
    )

    assert first.status_code == 200
    assert retry.status_code == 200
    assert retry.json()["incident_processing"] == {
        "created_count": 0,
        "updated_count": 0,
        "reopened_count": 0,
        "ignored_occurrence_count": 1,
    }

    incident_repository = SQLiteIncidentRepository(
        tmp_path / "registry.sqlite3"
    )
    incidents = incident_repository.list_incidents(
        IncidentFilters(dataset_id="customers")
    )
    assert len(incidents) == 1
    assert incidents[0].occurrence_count == 1
    occurrences = incident_repository.list_occurrences(incidents[0].incident_id)
    assert [occurrence.analysis_id for occurrence in occurrences] == [
        "same-analysis-key"
    ]


def test_different_idempotency_key_records_a_new_analysis_occurrence(
    api_client,
    tmp_path,
):
    client, _ = api_client
    upload_csv(
        client,
        "customers",
        b"id,name\n1,Ana",
        idempotency_key="different-key-baseline",
    )
    first = upload_csv(
        client,
        "customers",
        b"id,name,email\n2,Bruno,bruno@example.com",
        idempotency_key="analysis-key-one",
    )
    second = upload_csv(
        client,
        "customers",
        b"id,name,email\n3,Carlos,carlos@example.com",
        idempotency_key="analysis-key-two",
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["incident_processing"]["updated_count"] == 1

    incident_repository = SQLiteIncidentRepository(
        tmp_path / "registry.sqlite3"
    )
    incidents = incident_repository.list_incidents(
        IncidentFilters(dataset_id="customers")
    )
    assert len(incidents) == 1
    assert incidents[0].occurrence_count == 2
    occurrences = incident_repository.list_occurrences(incidents[0].incident_id)
    assert {occurrence.analysis_id for occurrence in occurrences} == {
        "analysis-key-one",
        "analysis-key-two",
    }


def test_incidents_persist_across_app_instances_sharing_database(tmp_path):
    database_path = tmp_path / "shared.sqlite3"
    first_schema_repository = SQLiteSchemaRegistryRepository(database_path)
    first_incident_repository = SQLiteIncidentRepository(database_path)
    first_app = create_app(
        repository=first_schema_repository,
        incident_repository=first_incident_repository,
        auth_repository=SQLiteAuthRepository(database_path),
    )
    with TestClient(first_app, base_url="https://testserver") as first_client:
        authenticate_admin(first_app, first_client, "shared-first-admin")
        first_response = upload_csv(
            first_client,
            "customers",
            b"id,name\n1,",
            idempotency_key="persistent-incident-one",
        )

    assert first_response.status_code == 200
    assert first_response.json()["incident_processing"]["created_count"] == 1

    second_schema_repository = SQLiteSchemaRegistryRepository(database_path)
    second_incident_repository = SQLiteIncidentRepository(database_path)
    second_app = create_app(
        repository=second_schema_repository,
        incident_repository=second_incident_repository,
        auth_repository=SQLiteAuthRepository(database_path),
    )
    with TestClient(second_app, base_url="https://testserver") as second_client:
        authenticate_admin(second_app, second_client, "shared-second-admin")
        second_response = upload_csv(
            second_client,
            "customers",
            b"id,name\n2,",
            idempotency_key="persistent-incident-two",
        )

    assert second_response.status_code == 200
    assert second_response.json()["incident_processing"]["updated_count"] == 1
    incidents = second_incident_repository.list_incidents(
        IncidentFilters(dataset_id="customers")
    )
    assert len(incidents) == 1
    assert incidents[0].occurrence_count == 2


def test_incident_persistence_failure_is_not_reported_as_success_and_retry_works(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "failure.sqlite3"
    schema_repository = SQLiteSchemaRegistryRepository(database_path)
    incident_repository = SQLiteIncidentRepository(database_path)
    app = create_app(
        repository=schema_repository,
        incident_repository=incident_repository,
        auth_repository=SQLiteAuthRepository(database_path),
    )
    original_record_status_change = SQLiteIncidentUnitOfWork.record_status_change
    should_fail = True

    def fail_once(unit_of_work, change):
        nonlocal should_fail
        if should_fail:
            should_fail = False
            raise RuntimeError("Injected incident persistence failure.")
        return original_record_status_change(unit_of_work, change)

    monkeypatch.setattr(
        SQLiteIncidentUnitOfWork,
        "record_status_change",
        fail_once,
    )

    with TestClient(app, base_url="https://testserver") as client:
        authenticate_admin(app, client, "persistence-failure-admin")
        failed_response = upload_csv(
            client,
            "customers",
            b"id,name\n1,",
            idempotency_key="retry-after-persistence-failure",
        )
        assert schema_repository.get_current("customers") is None
        assert incident_repository.list_incidents(
            IncidentFilters(dataset_id="customers")
        ) == []
        retry_response = upload_csv(
            client,
            "customers",
            b"id,name\n1,",
            idempotency_key="retry-after-persistence-failure",
        )

    assert failed_response.status_code == 500
    assert retry_response.status_code == 200
    assert schema_repository.get_current("customers") is not None
    assert retry_response.json()["incident_processing"]["created_count"] == 1
    incidents = incident_repository.list_incidents(
        IncidentFilters(dataset_id="customers")
    )
    assert len(incidents) == 1
    assert incidents[0].occurrence_count == 1


def test_create_app_accepts_explicit_analysis_repository(tmp_path):
    database_path = tmp_path / "injected.sqlite3"
    schema_repository = SQLiteSchemaRegistryRepository(database_path)
    incident_repository = SQLiteIncidentRepository(database_path)
    analysis_repository = SQLiteDatasetAnalysisRepository(
        database_path,
        schema_repository,
        incident_repository,
    )

    app = create_app(
        repository=schema_repository,
        incident_repository=incident_repository,
        dataset_analysis_repository=analysis_repository,
        auth_repository=SQLiteAuthRepository(database_path),
    )

    assert app.state.dataset_analysis_repository is analysis_repository


def test_create_app_requires_aggregate_for_non_sqlite_repositories():
    with pytest.raises(ValueError, match="dataset_analysis_repository is required"):
        create_app(
            repository=cast(SchemaRegistryRepository, object()),
            incident_repository=cast(IncidentRepository, object()),
        )


def test_analyze_rejects_missing_dataset_id(api_client):
    client, _ = api_client

    response = client.post(
        "/datasets/analyze",
        files={"file": ("customers.csv", b"id\n1", "text/csv")},
        headers={"Idempotency-Key": "missing-dataset-id"},
    )

    assert response.status_code == 422


def test_analyze_rejects_invalid_dataset_id(api_client):
    client, _ = api_client

    response = upload_csv(
        client,
        " ",
        b"id\n1",
        idempotency_key="invalid-dataset-id",
    )

    assert response.status_code == 400
    assert "dataset_id" in response.json()["detail"]


def test_analyze_rejects_non_csv(api_client):
    client, _ = api_client

    response = upload_csv(
        client,
        "customers",
        b"invalid content",
        idempotency_key="non-csv-upload",
        filename="customers.xlsx",
    )

    assert response.status_code == 400


def test_analyze_rejects_empty_file(api_client):
    client, _ = api_client

    response = upload_csv(
        client,
        "customers",
        b"",
        idempotency_key="empty-upload",
    )

    assert response.status_code == 400


def test_analyze_rejects_large_file(api_client):
    client, _ = api_client
    content = b"x" * (MAX_UPLOAD_SIZE + 1)

    response = upload_csv(
        client,
        "customers",
        content,
        idempotency_key="oversized-upload",
    )

    assert response.status_code == 413


def test_analyze_rejects_excessive_column_count_as_payload_too_large(api_client):
    client, _ = api_client
    header = ",".join(
        f"column_{index}" for index in range(MAX_ANALYSIS_COLUMNS + 1)
    )
    row = ",".join("value" for _ in range(MAX_ANALYSIS_COLUMNS + 1))

    response = upload_csv(
        client,
        "customers",
        f"{header}\n{row}\n".encode(),
        idempotency_key="too-many-columns",
    )

    assert response.status_code == 413
    assert "columns" in response.json()["detail"]


@pytest.mark.anyio
async def test_concurrent_analysis_returns_503_and_first_request_completes(
    tmp_path,
    monkeypatch,
):
    first_database_path = tmp_path / "first-analysis.sqlite3"
    first_repository = SQLiteSchemaRegistryRepository(first_database_path)
    first_incident_repository = SQLiteIncidentRepository(first_database_path)
    first_app = create_app(
        repository=first_repository,
        incident_repository=first_incident_repository,
        auth_repository=SQLiteAuthRepository(first_database_path),
    )
    second_database_path = tmp_path / "second-analysis.sqlite3"
    second_repository = SQLiteSchemaRegistryRepository(second_database_path)
    second_incident_repository = SQLiteIncidentRepository(second_database_path)
    second_app = create_app(
        repository=second_repository,
        incident_repository=second_incident_repository,
        auth_repository=SQLiteAuthRepository(second_database_path),
    )
    started = Event()
    release = Event()
    block_first_analysis(monkeypatch, started, release)

    first_transport = ASGITransport(app=first_app)
    second_transport = ASGITransport(app=second_app)
    async with AsyncClient(
        transport=first_transport,
        base_url="https://first",
    ) as first_client, AsyncClient(
        transport=second_transport,
        base_url="https://second",
    ) as second_client:
        first_app.state.auth_service.create_user(
            "concurrent-first-admin",
            "concurrent-first-admin-password",
            role=UserRole.ADMIN,
        )
        first_session = first_app.state.auth_service.authenticate(
            "concurrent-first-admin",
            "concurrent-first-admin-password",
        )
        first_client.cookies.set(
            SESSION_COOKIE_NAME,
            first_session.session_token,
        )
        second_app.state.auth_service.create_user(
            "concurrent-second-admin",
            "concurrent-second-admin-password",
            role=UserRole.ADMIN,
        )
        second_session = second_app.state.auth_service.authenticate(
            "concurrent-second-admin",
            "concurrent-second-admin-password",
        )
        second_client.cookies.set(
            SESSION_COOKIE_NAME,
            second_session.session_token,
        )
        first_request_finished = anyio.Event()
        first_response = []

        async def send_first_request():
            try:
                first_response.append(
                    await upload_csv_async(
                        first_client,
                        "customers",
                        b"id,name\n1,Ana",
                        idempotency_key="blocked-analysis",
                    )
                )
            finally:
                first_request_finished.set()

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(send_first_request)
            try:
                assert await anyio.to_thread.run_sync(started.wait, 5)
                second_response = await upload_csv_async(
                    second_client,
                    "customers",
                    b"id,name\n2,Bruno",
                    idempotency_key="concurrent-analysis",
                )
            finally:
                release.set()

            await first_request_finished.wait()

    assert second_response.status_code == 503
    assert second_response.headers["Retry-After"] == "1"
    assert first_response[0].status_code == 200


def test_processing_exception_releases_analysis_semaphore(
    api_client,
    monkeypatch,
):
    client, _ = api_client
    original_process = main_module._process_dataset_analysis
    should_fail = True
    lock = Lock()

    def fail_once(*args, **kwargs):
        nonlocal should_fail
        with lock:
            fail_this_call = should_fail
            should_fail = False

        if fail_this_call:
            raise RuntimeError("Injected analysis processing failure.")
        return original_process(*args, **kwargs)

    monkeypatch.setattr(main_module, "_process_dataset_analysis", fail_once)

    failed_response = upload_csv(
        client,
        "customers",
        b"id,name\n1,Ana",
        idempotency_key="analysis-fails-once",
    )
    retry_response = upload_csv(
        client,
        "customers",
        b"id,name\n1,Ana",
        idempotency_key="analysis-after-failure",
    )

    assert failed_response.status_code == 500
    assert retry_response.status_code == 200


@pytest.mark.anyio
async def test_cancelled_request_keeps_semaphore_until_worker_finishes(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "cancelled-analysis.sqlite3"
    repository = SQLiteSchemaRegistryRepository(database_path)
    incident_repository = SQLiteIncidentRepository(database_path)
    app = create_app(
        repository=repository,
        incident_repository=incident_repository,
        auth_repository=SQLiteAuthRepository(database_path),
    )
    started = Event()
    release = Event()
    block_first_analysis(monkeypatch, started, release)
    transport = ASGITransport(app=app)

    async with AsyncClient(
        transport=transport,
        base_url="https://test",
    ) as client:
        app.state.auth_service.create_user(
            "cancelled-analysis-admin",
            "cancelled-analysis-admin-password",
            role=UserRole.ADMIN,
        )
        authenticated_session = app.state.auth_service.authenticate(
            "cancelled-analysis-admin",
            "cancelled-analysis-admin-password",
        )
        client.cookies.set(
            SESSION_COOKIE_NAME,
            authenticated_session.session_token,
        )
        first_request_scope = anyio.CancelScope()
        first_request_finished = anyio.Event()

        async def send_first_request():
            try:
                with first_request_scope:
                    await client.post(
                        "/datasets/analyze",
                        data={"dataset_id": "customers"},
                        files={
                            "file": (
                                "customers.csv",
                                b"id,name\n1,Ana",
                                "text/csv",
                            )
                        },
                        headers={"Idempotency-Key": "cancelled-analysis"},
                    )
            finally:
                first_request_finished.set()

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(send_first_request)
            try:
                assert await anyio.to_thread.run_sync(started.wait, 5)
                first_request_scope.cancel()

                second_response = await client.post(
                    "/datasets/analyze",
                    data={"dataset_id": "customers"},
                    files={
                        "file": (
                            "customers.csv",
                            b"id,name\n2,Bruno",
                            "text/csv",
                        )
                    },
                    headers={"Idempotency-Key": "during-cancellation"},
                )

                assert second_response.status_code == 503
                assert second_response.headers["Retry-After"] == "1"
                assert not first_request_finished.is_set()
            finally:
                release.set()

            await first_request_finished.wait()

        after_completion = await client.post(
            "/datasets/analyze",
            data={"dataset_id": "customers"},
            files={
                "file": (
                    "customers.csv",
                    b"id,name\n3,Carlos",
                    "text/csv",
                )
            },
            headers={"Idempotency-Key": "after-cancellation"},
        )

    assert after_completion.status_code == 200


def test_create_app_uses_configured_database_path(tmp_path, monkeypatch):
    database_path = tmp_path / "nested" / "registry.sqlite3"
    monkeypatch.setenv("VIGIA_DATABASE_PATH", str(database_path))

    app = create_app()
    with TestClient(app, base_url="https://testserver") as client:
        authenticate_admin(app, client, "configured-path-admin")
        baseline_response = upload_csv(
            client,
            "customers",
            b"id,name\n1,Ana",
            idempotency_key="default-path-baseline",
        )
        drift_response = upload_csv(
            client,
            "customers",
            b"id,name,email\n2,Bruno,bruno@example.com",
            idempotency_key="default-path-drift",
        )

    assert baseline_response.status_code == 200
    assert drift_response.status_code == 200
    assert database_path.exists()
    schema_repository = SQLiteSchemaRegistryRepository(database_path)
    incident_repository = SQLiteIncidentRepository(database_path)
    assert schema_repository.get_current("customers") is not None
    incidents = incident_repository.list_incidents(
        IncidentFilters(dataset_id="customers")
    )
    assert len(incidents) == 1


def test_baseline_persists_across_application_instances(tmp_path):
    database_path = tmp_path / "nested" / "registry.sqlite3"
    first_repository = SQLiteSchemaRegistryRepository(database_path)
    first_incident_repository = SQLiteIncidentRepository(database_path)
    first_app = create_app(
        repository=first_repository,
        incident_repository=first_incident_repository,
        auth_repository=SQLiteAuthRepository(database_path),
    )

    with TestClient(first_app, base_url="https://testserver") as first_client:
        authenticate_admin(first_app, first_client, "persistent-first-admin")
        first_response = upload_csv(
            first_client,
            "customers",
            b"id,name\n1,Ana",
            idempotency_key="persistent-baseline",
        )

    assert first_response.status_code == 200
    first_result = first_response.json()
    assert first_result["baseline_created"] is True
    assert first_result["baseline_version"] == 1
    assert database_path.parent.is_dir()

    del first_client
    del first_app
    del first_repository
    del first_incident_repository

    second_repository = SQLiteSchemaRegistryRepository(database_path)
    second_incident_repository = SQLiteIncidentRepository(database_path)
    second_app = create_app(
        repository=second_repository,
        incident_repository=second_incident_repository,
        auth_repository=SQLiteAuthRepository(database_path),
    )

    with TestClient(second_app, base_url="https://testserver") as second_client:
        authenticate_admin(second_app, second_client, "persistent-second-admin")
        second_response = upload_csv(
            second_client,
            "customers",
            b"id,name\n2,Bruno",
            idempotency_key="persistent-reload",
            filename="customers-reloaded.csv",
        )

    assert second_response.status_code == 200
    second_result = second_response.json()
    assert second_result["baseline_created"] is False
    assert second_result["baseline_version"] == 1
    assert second_result["schema_drift_issues"] == []


def test_schema_approval_creates_new_version_and_history(api_client):
    client, _ = api_client
    upload_csv(
        client,
        "customers",
        b"id,name\n1,Ana",
        idempotency_key="schema-approval-baseline",
    )

    approval_response = client.put(
        "/datasets/customers/schema",
        json={
            "schema": {"id": "int64", "name": "object", "email": "object"},
            "expected_version": 1,
        },
    )

    assert approval_response.status_code == 200
    approved = approval_response.json()
    assert approved["dataset_id"] == "customers"
    assert approved["version"] == 2
    assert approved["schema"] == {
        "id": "int64",
        "name": "object",
        "email": "object",
    }
    assert approved["created_at"]

    history_response = client.get("/datasets/customers/schema/history")

    assert history_response.status_code == 200
    history = history_response.json()
    assert history["dataset_id"] == "customers"
    assert [version["version"] for version in history["versions"]] == [1, 2]


def test_stale_schema_approval_returns_conflict(api_client):
    client, _ = api_client
    upload_csv(
        client,
        "customers",
        b"id\n1",
        idempotency_key="stale-approval-baseline",
    )

    first_approval = client.put(
        "/datasets/customers/schema",
        json={"schema": {"id": "int64", "name": "object"}, "expected_version": 1},
    )
    stale_approval = client.put(
        "/datasets/customers/schema",
        json={"schema": {"id": "object"}, "expected_version": 1},
    )

    assert first_approval.status_code == 200
    assert stale_approval.status_code == 409


def test_schema_approval_for_unknown_dataset_returns_not_found(api_client):
    client, _ = api_client

    response = client.put(
        "/datasets/unknown/schema",
        json={"schema": {"id": "int64"}, "expected_version": 1},
    )

    assert response.status_code == 404


def test_schema_history_for_unknown_dataset_returns_not_found(api_client):
    client, _ = api_client

    response = client.get("/datasets/unknown/schema/history")

    assert response.status_code == 404


def test_create_app_rejects_partial_repository_injection(tmp_path):
    database_path = tmp_path / "registry.sqlite3"
    schema_repository = SQLiteSchemaRegistryRepository(database_path)
    incident_repository = SQLiteIncidentRepository(database_path)

    with pytest.raises(ValueError, match="provided together"):
        create_app(repository=schema_repository)

    with pytest.raises(ValueError, match="provided together"):
        create_app(incident_repository=incident_repository)


def test_schema_approval_rejects_invalid_request_body(api_client):
    client, _ = api_client

    response = client.put(
        "/datasets/customers/schema",
        json={"schema": {"id": 123}, "expected_version": 1},
    )

    assert response.status_code == 422


def test_list_incidents_applies_combined_filters_and_pagination(
    api_client,
    tmp_path,
):
    client, _ = api_client
    incident_repository = SQLiteIncidentRepository(tmp_path / "registry.sqlite3")
    matching_older = make_api_incident(
        "matching-older",
        updated_at=API_TEST_TIME + timedelta(minutes=1),
    )
    matching_newer = make_api_incident(
        "matching-newer",
        updated_at=API_TEST_TIME + timedelta(minutes=2),
    )
    store_api_incident(incident_repository, matching_older)
    store_api_incident(incident_repository, matching_newer)
    store_api_incident(
        incident_repository,
        make_api_incident("other-dataset", dataset_id="orders"),
    )
    store_api_incident(
        incident_repository,
        make_api_incident(
            "other-status",
            status=IncidentStatus.ACKNOWLEDGED,
        ),
    )
    store_api_incident(
        incident_repository,
        make_api_incident(
            "other-severity",
            severity=IncidentSeverity.MEDIUM,
        ),
    )
    store_api_incident(
        incident_repository,
        make_api_incident("other-code", code="DUPLICATE_VALUES"),
    )

    response = client.get(
        "/incidents",
        params={
            "dataset_id": "customers",
            "status": "OPEN",
            "severity": "high",
            "code": "NULL_VALUES",
            "limit": 1,
            "offset": 1,
        },
    )

    assert response.status_code == 200
    result = response.json()
    assert set(result) == {"items", "limit", "offset"}
    assert [item["incident_id"] for item in result["items"]] == [
        "matching-older"
    ]
    assert result["limit"] == 1
    assert result["offset"] == 1


def test_list_incidents_returns_empty_filtered_collection(api_client):
    client, _ = api_client

    response = client.get(
        "/incidents",
        params={"dataset_id": "unknown-dataset"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "items": [],
        "limit": 50,
        "offset": 0,
    }


def test_get_incident_returns_existing_incident_and_404_for_missing(
    api_client,
    tmp_path,
):
    client, _ = api_client
    incident_repository = SQLiteIncidentRepository(tmp_path / "registry.sqlite3")
    incident = make_api_incident("detail-incident")
    store_api_incident(incident_repository, incident)

    response = client.get(f"/incidents/{incident.incident_id}")
    missing_response = client.get("/incidents/missing-incident")

    assert response.status_code == 200
    result = response.json()
    assert result["incident_id"] == incident.incident_id
    assert result["dataset_id"] == incident.dataset_id
    assert result["status"] == incident.status.value
    assert result["revision"] == incident.revision
    assert missing_response.status_code == 404


def test_incident_occurrences_and_history_return_requested_pages(
    api_client,
    tmp_path,
):
    client, _ = api_client
    incident_repository = SQLiteIncidentRepository(tmp_path / "registry.sqlite3")
    incident = make_api_incident("paged-incident")
    older_occurrence = make_api_occurrence(
        "occurrence-older",
        incident.incident_id,
        API_TEST_TIME,
    )
    newer_occurrence = make_api_occurrence(
        "occurrence-newer",
        incident.incident_id,
        API_TEST_TIME + timedelta(minutes=1),
    )
    first_change = make_api_status_change(
        "change-initial",
        incident.incident_id,
        None,
        IncidentStatus.OPEN,
        API_TEST_TIME,
    )
    second_change = make_api_status_change(
        "change-acknowledged",
        incident.incident_id,
        IncidentStatus.OPEN,
        IncidentStatus.ACKNOWLEDGED,
        API_TEST_TIME + timedelta(minutes=1),
    )
    store_api_incident(
        incident_repository,
        incident,
        occurrences=(older_occurrence, newer_occurrence),
        history=(first_change, second_change),
    )

    occurrences_response = client.get(
        f"/incidents/{incident.incident_id}/occurrences",
        params={"limit": 1, "offset": 1},
    )
    history_response = client.get(
        f"/incidents/{incident.incident_id}/history",
        params={"limit": 1, "offset": 1},
    )

    assert occurrences_response.status_code == 200
    occurrences = occurrences_response.json()
    assert set(occurrences) == {"items", "limit", "offset"}
    assert [item["occurrence_id"] for item in occurrences["items"]] == [
        "occurrence-older"
    ]
    assert occurrences["items"][0]["examples"] == [1, 3]
    assert occurrences["limit"] == 1
    assert occurrences["offset"] == 1

    assert history_response.status_code == 200
    history = history_response.json()
    assert set(history) == {"items", "limit", "offset"}
    assert [item["change_id"] for item in history["items"]] == [
        "change-acknowledged"
    ]
    assert history["limit"] == 1
    assert history["offset"] == 1


def test_empty_incident_occurrences_and_history_return_empty_items(
    api_client,
    tmp_path,
):
    client, _ = api_client
    incident_repository = SQLiteIncidentRepository(tmp_path / "registry.sqlite3")
    incident = make_api_incident("empty-history-incident")
    store_api_incident(incident_repository, incident)

    occurrences_response = client.get(
        f"/incidents/{incident.incident_id}/occurrences"
    )
    history_response = client.get(f"/incidents/{incident.incident_id}/history")

    assert occurrences_response.status_code == 200
    assert occurrences_response.json()["items"] == []
    assert history_response.status_code == 200
    assert history_response.json()["items"] == []


@pytest.mark.parametrize("resource", ["occurrences", "history"])
def test_incident_occurrences_and_history_return_404_for_missing_parent(
    api_client,
    resource,
):
    client, _ = api_client

    response = client.get(f"/incidents/missing-incident/{resource}")

    assert response.status_code == 404


def test_patch_incident_status_updates_revision_and_records_audit_fields(
    api_client,
    tmp_path,
):
    client, _ = api_client
    incident_repository = SQLiteIncidentRepository(tmp_path / "registry.sqlite3")
    incident = make_api_incident("status-change-incident")
    store_api_incident(incident_repository, incident)

    reason = "  Reviewed by the on-call operator.  "
    response = client.patch(
        f"/incidents/{incident.incident_id}/status",
        json={
            "status": "ACKNOWLEDGED",
            "expected_revision": incident.revision,
            "reason": reason,
        },
    )

    assert response.status_code == 200
    result = response.json()
    assert result["status"] == IncidentStatus.ACKNOWLEDGED.value
    assert result["revision"] == incident.revision + 1

    persisted = incident_repository.get_incident(incident.incident_id)
    history = incident_repository.list_status_history(incident.incident_id)
    assert persisted is not None
    assert persisted.status is IncidentStatus.ACKNOWLEDGED
    assert persisted.revision == incident.revision + 1
    assert len(history) == 1
    assert history[0].actor == "api"
    assert history[0].reason == reason


def test_patch_repeating_current_status_is_successful_no_op(
    api_client,
    tmp_path,
):
    client, _ = api_client
    incident_repository = SQLiteIncidentRepository(tmp_path / "registry.sqlite3")
    incident = make_api_incident(
        "unchanged-status-incident",
        status=IncidentStatus.ACKNOWLEDGED,
    )
    existing_change = make_api_status_change(
        "existing-acknowledged-change",
        incident.incident_id,
        None,
        IncidentStatus.ACKNOWLEDGED,
        API_TEST_TIME,
    )
    store_api_incident(
        incident_repository,
        incident,
        history=(existing_change,),
    )

    response = client.patch(
        f"/incidents/{incident.incident_id}/status",
        json={
            "status": IncidentStatus.ACKNOWLEDGED.value,
            "expected_revision": incident.revision,
            "reason": "No transition requested.",
        },
    )

    assert response.status_code == 200
    assert response.json()["revision"] == incident.revision
    assert incident_repository.get_incident(incident.incident_id) == incident
    assert incident_repository.list_status_history(incident.incident_id) == [
        existing_change
    ]


def test_patch_invalid_status_transition_returns_conflict_without_changes(
    api_client,
    tmp_path,
):
    client, _ = api_client
    incident_repository = SQLiteIncidentRepository(tmp_path / "registry.sqlite3")
    incident = make_api_incident(
        "resolved-incident",
        status=IncidentStatus.RESOLVED,
        resolved_at=API_TEST_TIME,
    )
    store_api_incident(incident_repository, incident)

    response = client.patch(
        f"/incidents/{incident.incident_id}/status",
        json={
            "status": IncidentStatus.OPEN.value,
            "expected_revision": incident.revision,
            "reason": "Manual reopen attempt.",
        },
    )

    assert response.status_code == 409
    assert incident_repository.get_incident(incident.incident_id) == incident
    assert incident_repository.list_status_history(incident.incident_id) == []


def test_patch_missing_incident_returns_404(api_client):
    client, _ = api_client

    response = client.patch(
        "/incidents/missing-incident/status",
        json={
            "status": IncidentStatus.ACKNOWLEDGED.value,
            "expected_revision": 1,
            "reason": "Incident should not exist.",
        },
    )

    assert response.status_code == 404


def test_patch_stale_revision_returns_conflict_with_revision_details(
    api_client,
    tmp_path,
):
    client, _ = api_client
    incident_repository = SQLiteIncidentRepository(tmp_path / "registry.sqlite3")
    incident = make_api_incident("stale-revision-incident")
    store_api_incident(incident_repository, incident)

    response = client.patch(
        f"/incidents/{incident.incident_id}/status",
        json={
            "status": IncidentStatus.ACKNOWLEDGED.value,
            "expected_revision": incident.revision + 1,
            "reason": "Stale request.",
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"]["expected_revision"] == incident.revision + 1
    assert response.json()["detail"]["current_revision"] == incident.revision
    assert incident_repository.get_incident(incident.incident_id) == incident
    assert incident_repository.list_status_history(incident.incident_id) == []


@pytest.mark.parametrize(
    ("parameter", "value"),
    [
        ("dataset_id", " "),
        ("code", " "),
        ("status", "UNKNOWN"),
        ("severity", "urgent"),
        ("limit", 0),
        ("limit", 101),
        ("offset", -1),
    ],
)
def test_list_incidents_rejects_invalid_filters_and_pagination(
    api_client,
    parameter,
    value,
):
    client, _ = api_client

    response = client.get("/incidents", params={parameter: value})

    assert response.status_code == 422


@pytest.mark.parametrize(
    "body",
    [
        {
            "status": "UNKNOWN",
            "expected_revision": 1,
            "reason": "Invalid status.",
        },
        {
            "status": "ACKNOWLEDGED",
            "expected_revision": 0,
            "reason": "Invalid revision.",
        },
        {
            "status": "ACKNOWLEDGED",
            "expected_revision": -1,
            "reason": "Negative revision.",
        },
        {
            "status": "ACKNOWLEDGED",
            "expected_revision": True,
            "reason": "Boolean revision.",
        },
        {
            "status": "ACKNOWLEDGED",
            "expected_revision": "1",
            "reason": "String revision.",
        },
        {
            "status": "ACKNOWLEDGED",
            "expected_revision": 1,
            "reason": " \t ",
        },
        {
            "status": "ACKNOWLEDGED",
            "expected_revision": 1,
        },
        {
            "status": "ACKNOWLEDGED",
            "expected_revision": 1,
            "reason": "Client actor must not be trusted.",
            "actor": "client-supplied",
        },
    ],
)
def test_patch_rejects_invalid_request_bodies(api_client, body):
    client, _ = api_client

    response = client.patch(
        "/incidents/any-incident/status",
        json=body,
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    ("resource", "params"),
    [
        ("occurrences", {"limit": 0}),
        ("history", {"offset": -1}),
    ],
)
def test_occurrence_and_history_endpoints_reject_invalid_pagination(
    api_client,
    resource,
    params,
):
    client, _ = api_client

    response = client.get(
        f"/incidents/any-incident/{resource}",
        params=params,
    )

    assert response.status_code == 422


def make_authorized_request(
    client: TestClient,
    method: str,
    path: str,
    *,
    json=None,
    data=None,
    files=None,
    headers=None,
    params=None,
):
    return client.request(
        method,
        path,
        json=json,
        data=data,
        files=files,
        headers=headers,
        params=params,
    )


@pytest.mark.parametrize(
    ("method", "path", "kwargs"),
    [
        ("GET", "/incidents", {}),
        ("GET", "/incidents/one", {}),
        ("GET", "/incidents/one/occurrences", {}),
        ("GET", "/incidents/one/history", {}),
        (
            "PATCH",
            "/incidents/one/status",
            {
                "json": {
                    "status": "ACKNOWLEDGED",
                    "expected_revision": 1,
                    "reason": "Authorization test.",
                }
            },
        ),
        (
            "POST",
            "/datasets/analyze",
            {
                "data": {"dataset_id": "customers"},
                "files": {
                    "file": ("customers.csv", b"id\n1", "text/csv"),
                },
                "headers": {"Idempotency-Key": "authorization-no-session"},
            },
        ),
        (
            "PUT",
            "/datasets/customers/schema",
            {
                "json": {
                    "schema": {"id": "int64"},
                    "expected_version": 1,
                }
            },
        ),
        ("GET", "/datasets/customers/schema/history", {}),
    ],
    ids=[
        "list-incidents",
        "get-incident",
        "incident-occurrences",
        "incident-history",
        "change-status",
        "analyze-dataset",
        "approve-schema",
        "schema-history",
    ],
)
def test_protected_business_routes_return_401_without_session(
    authorization_api,
    method,
    path,
    kwargs,
):
    _, client, _, _, _ = authorization_api
    client.cookies.clear()

    response = make_authorized_request(client, method, path, **kwargs)

    assert response.status_code == 401


def test_protected_business_route_returns_401_for_invalid_cookie(
    authorization_api,
):
    _, client, _, _, _ = authorization_api
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE_NAME, "invalid-session-token")

    response = client.get("/incidents")

    assert response.status_code == 401


def test_protected_business_route_returns_401_for_revoked_session(
    authorization_api,
):
    app, client, _, _, passwords = authorization_api
    authenticated_session = authenticate_api_role(
        app,
        client,
        UserRole.ADMIN,
        passwords,
    )
    assert app.state.auth_service.logout(
        authenticated_session.session_token
    )

    response = client.get("/incidents")

    assert response.status_code == 401


def test_viewer_cannot_mutate_without_creating_any_resource(
    authorization_api,
):
    app, client, repository, incident_repository, passwords = authorization_api
    authenticate_api_role(
        app,
        client,
        UserRole.VIEWER,
        passwords,
    )

    incident = make_api_incident("viewer-denied-incident")
    store_api_incident(incident_repository, incident)
    repository.get_or_create_initial("customers", {"id": "int64"})

    analyze_response = client.post(
        "/datasets/analyze",
        data={"dataset_id": "viewer-denied"},
        files={
            "file": (
                "viewer-denied.csv",
                b"id,name\n1,Ana",
                "text/csv",
            ),
        },
        headers={"Idempotency-Key": "viewer-denied-analysis"},
    )
    approve_response = client.put(
        "/datasets/customers/schema",
        json={
            "schema": {"id": "int64", "name": "str"},
            "expected_version": 1,
        },
    )
    status_response = client.patch(
        f"/incidents/{incident.incident_id}/status",
        json={
            "status": "ACKNOWLEDGED",
            "expected_revision": 1,
            "reason": "Viewer must be denied.",
        },
    )

    assert analyze_response.status_code == 403
    assert approve_response.status_code == 403
    assert status_response.status_code == 403
    assert repository.get_current("viewer-denied") is None
    assert incident_repository.get_incident(incident.incident_id) == incident
    assert incident_repository.list_status_history(
        incident.incident_id
    ) == []
    assert repository.get_current("customers").version == 1
    assert repository.get_current("customers").schema == {"id": "int64"}


def test_viewer_can_read_schema_history(
    authorization_api,
):
    app, client, repository, _, passwords = authorization_api
    authenticate_api_role(
        app,
        client,
        UserRole.VIEWER,
        passwords,
    )
    baseline = repository.get_or_create_initial(
        "viewer-history",
        {"id": "int64", "name": "str"},
    )[0]

    response = client.get("/datasets/viewer-history/schema/history")

    assert response.status_code == 200
    assert response.json() == {
        "dataset_id": "viewer-history",
        "versions": [
            {
                "version": baseline.version,
                "schema": baseline.schema,
                "created_at": baseline.created_at,
            }
        ],
    }


@pytest.mark.parametrize(
    ("role", "operation", "expected_status"),
    [
        (UserRole.VIEWER, "analyze", 403),
        (UserRole.VIEWER, "approve", 403),
        (UserRole.VIEWER, "status", 403),
        (UserRole.ANALYST, "analyze", 200),
        (UserRole.ANALYST, "approve", 403),
        (UserRole.ANALYST, "status", 403),
        (UserRole.INCIDENT_OPERATOR, "analyze", 200),
        (UserRole.INCIDENT_OPERATOR, "approve", 403),
        (UserRole.INCIDENT_OPERATOR, "status", 200),
        (UserRole.ADMIN, "analyze", 200),
        (UserRole.ADMIN, "approve", 200),
        (UserRole.ADMIN, "status", 200),
    ],
)
def test_mutating_operation_role_permission_matrix(
    authorization_api,
    role,
    operation,
    expected_status,
):
    app, client, repository, incident_repository, passwords = authorization_api
    authenticate_api_role(app, client, role, passwords)

    if operation == "analyze":
        response = client.post(
            "/datasets/analyze",
            data={"dataset_id": f"{role.value}-dataset"},
            files={
                "file": (
                    f"{role.value}.csv",
                    b"id,name\n1,Ana",
                    "text/csv",
                ),
            },
            headers={
                "Idempotency-Key": f"{role.value}-{operation}",
            },
        )
        assert response.status_code == expected_status
        assert (
            repository.get_current(f"{role.value}-dataset") is not None
            if expected_status == 200
            else repository.get_current(f"{role.value}-dataset") is None
        )
        return

    if operation == "approve":
        baseline = repository.get_or_create_initial(
            f"{role.value}-schema",
            {"id": "int64"},
        )[0]
        response = client.put(
            f"/datasets/{role.value}-schema/schema",
            json={
                "schema": {"id": "int64", "name": "str"},
                "expected_version": baseline.version,
            },
        )
        assert response.status_code == expected_status
        current = repository.get_current(f"{role.value}-schema")
        assert current is not None
        assert (
            current.version == 2
            if expected_status == 200
            else current.version == 1
        )
        return

    incident = make_api_incident(f"{role.value}-status")
    store_api_incident(incident_repository, incident)
    response = client.patch(
        f"/incidents/{incident.incident_id}/status",
        json={
            "status": "ACKNOWLEDGED",
            "expected_revision": 1,
            "reason": f"{role.value} authorization test.",
        },
    )
    assert response.status_code == expected_status
    persisted = incident_repository.get_incident(incident.incident_id)
    history = incident_repository.list_status_history(incident.incident_id)
    assert persisted is not None
    assert (
        persisted.revision == 2
        and len(history) == 1
        if expected_status == 200
        else persisted.revision == 1
        and len(history) == 0
    )


def test_client_supplied_role_permission_and_identity_do_not_elevate(
    authorization_api,
):
    app, client, repository, incident_repository, passwords = authorization_api
    authenticate_api_role(
        app,
        client,
        UserRole.VIEWER,
        passwords,
    )

    incident = make_api_incident("query-role-spoof")
    store_api_incident(incident_repository, incident)
    query_spoof = client.patch(
        f"/incidents/{incident.incident_id}/status",
        params={
            "role": "admin",
            "permission": "incident:status:change",
        },
        json={
            "status": "ACKNOWLEDGED",
            "expected_revision": 1,
            "reason": "Query supplied role.",
        },
    )
    header_spoof = client.post(
        "/datasets/analyze",
        data={"dataset_id": "spoofed-dataset"},
        files={
            "file": (
                "spoofed.csv",
                b"id\n1",
                "text/csv",
            ),
        },
        headers={
            "Idempotency-Key": "role-spoof",
            "role": "admin",
            "permission": "schema:approve",
            "X-User-Id": "00000000-0000-0000-0000-000000000001",
        },
    )

    body_incident = make_api_incident("body-role-spoof")
    store_api_incident(incident_repository, body_incident)
    body_spoof = client.patch(
        f"/incidents/{body_incident.incident_id}/status",
        params={"role": "admin"},
        json={
            "status": "ACKNOWLEDGED",
            "expected_revision": 1,
            "reason": "Client supplied identity.",
            "role": "admin",
            "permission": "incident:status:change",
            "actor": "00000000-0000-0000-0000-000000000001",
        },
    )

    assert query_spoof.status_code == 403
    assert header_spoof.status_code == 403
    assert body_spoof.status_code == 403
    assert incident_repository.get_incident(
        incident.incident_id
    ) == incident
    assert incident_repository.list_status_history(
        incident.incident_id
    ) == []
    assert incident_repository.get_incident(
        body_incident.incident_id
    ) == body_incident
    assert incident_repository.list_status_history(
        body_incident.incident_id
    ) == []
    assert repository.get_current("spoofed-dataset") is None
