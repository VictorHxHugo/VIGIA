from fastapi.testclient import TestClient
import pytest

from app.incidents.repository import IncidentFilters
from app.incidents.sqlite_repository import SQLiteIncidentRepository
from app.main import MAX_UPLOAD_SIZE, create_app
from app.schema_registry.sqlite_repository import SQLiteSchemaRegistryRepository


@pytest.fixture
def api_client(tmp_path):
    database_path = tmp_path / "registry.sqlite3"
    repository = SQLiteSchemaRegistryRepository(database_path)
    incident_repository = SQLiteIncidentRepository(database_path)
    app = create_app(
        repository=repository,
        incident_repository=incident_repository,
    )

    with TestClient(app) as client:
        yield client, repository


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
    )
    with TestClient(first_app) as first_client:
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
    )
    with TestClient(second_app) as second_client:
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
    )
    original_transaction = incident_repository.transaction
    should_fail = True

    def fail_once():
        nonlocal should_fail
        if should_fail:
            should_fail = False
            raise RuntimeError("Injected incident persistence failure.")
        return original_transaction()

    monkeypatch.setattr(incident_repository, "transaction", fail_once)

    with TestClient(app) as client:
        failed_response = upload_csv(
            client,
            "customers",
            b"id,name\n1,",
            idempotency_key="retry-after-persistence-failure",
        )
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


def test_create_app_uses_configured_database_path(tmp_path, monkeypatch):
    database_path = tmp_path / "nested" / "registry.sqlite3"
    monkeypatch.setenv("VIGIA_DATABASE_PATH", str(database_path))

    app = create_app()
    with TestClient(app) as client:
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
    )

    with TestClient(first_app) as first_client:
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
    )

    with TestClient(second_app) as second_client:
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
