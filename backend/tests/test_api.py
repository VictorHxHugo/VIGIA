from fastapi.testclient import TestClient
import pytest

from app.main import MAX_UPLOAD_SIZE, create_app
from app.schema_registry.sqlite_repository import SQLiteSchemaRegistryRepository


@pytest.fixture
def api_client(tmp_path):
    repository = SQLiteSchemaRegistryRepository(tmp_path / "registry.sqlite3")
    app = create_app(repository=repository)

    with TestClient(app) as client:
        yield client, repository


def upload_csv(
    client: TestClient,
    dataset_id: str,
    content: bytes,
    filename: str = "customers.csv",
):
    return client.post(
        "/datasets/analyze",
        data={"dataset_id": dataset_id},
        files={"file": (filename, content, "text/csv")},
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

    response = upload_csv(client, "customers", content)

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
    baseline = repository.get_current("customers")
    assert baseline is not None
    assert baseline.version == 1


def test_second_upload_with_identical_schema_has_no_drift(api_client):
    client, _ = api_client
    first = upload_csv(client, "customers", b"id,name\n1,Ana")
    second = upload_csv(
        client,
        "customers",
        b"id,name\n2,Bruno",
        filename="customers-latest.csv",
    )

    assert first.status_code == 200
    assert second.status_code == 200
    result = second.json()
    assert result["baseline_version"] == 1
    assert result["baseline_created"] is False
    assert result["schema_drift_issues"] == []
    assert result["quality_report"]["dataset_name"] == "customers-latest.csv"


def test_upload_with_schema_drift_preserves_persisted_baseline(api_client):
    client, repository = api_client
    upload_csv(client, "customers", b"id,name\n1,Ana")

    response = upload_csv(
        client,
        "customers",
        b"id,name,email\n2,Bruno,bruno@example.com",
    )

    assert response.status_code == 200
    result = response.json()
    assert [issue["code"] for issue in result["schema_drift_issues"]] == [
        "COLUMN_ADDED"
    ]
    assert result["baseline_version"] == 1
    assert result["baseline_created"] is False

    baseline = repository.get_current("customers")
    assert baseline is not None
    assert baseline.version == 1
    assert baseline.schema == {"id": "int64", "name": "str"}


def test_analyze_rejects_missing_dataset_id(api_client):
    client, _ = api_client

    response = client.post(
        "/datasets/analyze",
        files={"file": ("customers.csv", b"id\n1", "text/csv")},
    )

    assert response.status_code == 422


def test_analyze_rejects_invalid_dataset_id(api_client):
    client, _ = api_client

    response = upload_csv(client, " ", b"id\n1")

    assert response.status_code == 400
    assert "dataset_id" in response.json()["detail"]


def test_analyze_rejects_non_csv(api_client):
    client, _ = api_client

    response = upload_csv(
        client,
        "customers",
        b"invalid content",
        filename="customers.xlsx",
    )

    assert response.status_code == 400


def test_analyze_rejects_empty_file(api_client):
    client, _ = api_client

    response = upload_csv(client, "customers", b"")

    assert response.status_code == 400


def test_analyze_rejects_large_file(api_client):
    client, _ = api_client
    content = b"x" * (MAX_UPLOAD_SIZE + 1)

    response = upload_csv(client, "customers", content)

    assert response.status_code == 413


def test_create_app_uses_configured_database_path(tmp_path, monkeypatch):
    database_path = tmp_path / "nested" / "registry.sqlite3"
    monkeypatch.setenv("VIGIA_DATABASE_PATH", str(database_path))

    app = create_app()
    app.state.schema_registry_service.get_or_create_baseline(
        "customers",
        {"id": "int64"},
    )

    assert database_path.exists()


def test_baseline_persists_across_application_instances(tmp_path):
    database_path = tmp_path / "nested" / "registry.sqlite3"
    first_repository = SQLiteSchemaRegistryRepository(database_path)
    first_app = create_app(repository=first_repository)

    with TestClient(first_app) as first_client:
        first_response = upload_csv(
            first_client,
            "customers",
            b"id,name\n1,Ana",
        )

    assert first_response.status_code == 200
    first_result = first_response.json()
    assert first_result["baseline_created"] is True
    assert first_result["baseline_version"] == 1
    assert database_path.parent.is_dir()

    del first_client
    del first_app
    del first_repository

    second_repository = SQLiteSchemaRegistryRepository(database_path)
    second_app = create_app(repository=second_repository)

    with TestClient(second_app) as second_client:
        second_response = upload_csv(
            second_client,
            "customers",
            b"id,name\n2,Bruno",
            filename="customers-reloaded.csv",
        )

    assert second_response.status_code == 200
    second_result = second_response.json()
    assert second_result["baseline_created"] is False
    assert second_result["baseline_version"] == 1
    assert second_result["schema_drift_issues"] == []


def test_schema_approval_creates_new_version_and_history(api_client):
    client, _ = api_client
    upload_csv(client, "customers", b"id,name\n1,Ana")

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
    upload_csv(client, "customers", b"id\n1")

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


def test_schema_approval_rejects_invalid_request_body(api_client):
    client, _ = api_client

    response = client.put(
        "/datasets/customers/schema",
        json={"schema": {"id": 123}, "expected_version": 1},
    )

    assert response.status_code == 422
