import pandas as pd
import pytest

from app.datasets.service import DatasetAnalysisService
from app.schema_registry.service import SchemaRegistryService
from app.schema_registry.sqlite_repository import SQLiteSchemaRegistryRepository


@pytest.fixture
def analysis_setup(tmp_path):
    repository = SQLiteSchemaRegistryRepository(tmp_path / "registry.sqlite3")
    registry_service = SchemaRegistryService(repository)
    analysis_service = DatasetAnalysisService(registry_service)
    return repository, registry_service, analysis_service


def test_first_analysis_creates_version_one_without_drift(analysis_setup):
    repository, _, analysis_service = analysis_setup

    result = analysis_service.analyze(
        pd.DataFrame({"id": [1, 2]}),
        dataset_id="customers",
        dataset_name="customers.csv",
    )

    assert result.dataset_id == "customers"
    assert result.baseline_version == 1
    assert result.baseline_created is True
    assert result.schema_drift_issues == []
    current = repository.get_current("customers")
    assert current is not None
    assert current.version == 1


def test_identical_followup_schema_has_no_drift(analysis_setup):
    _, _, analysis_service = analysis_setup
    first = analysis_service.analyze(
        pd.DataFrame({"id": [1, 2]}),
        "customers",
        "customers.csv",
    )

    second = analysis_service.analyze(
        pd.DataFrame({"id": [3, 4]}),
        "customers",
        "customers-next.csv",
    )

    assert first.baseline_created is True
    assert second.baseline_created is False
    assert second.baseline_version == 1
    assert second.schema_drift_issues == []


def test_added_column_is_reported_as_schema_drift(analysis_setup):
    _, _, analysis_service = analysis_setup
    analysis_service.analyze(
        pd.DataFrame({"id": [1]}),
        "customers",
        "customers.csv",
    )

    result = analysis_service.analyze(
        pd.DataFrame({"id": [2], "email": ["ana@example.com"]}),
        "customers",
        "customers.csv",
    )

    assert [(issue.code, issue.column) for issue in result.schema_drift_issues] == [
        ("COLUMN_ADDED", "email")
    ]


def test_removed_column_is_reported_as_schema_drift(analysis_setup):
    _, _, analysis_service = analysis_setup
    analysis_service.analyze(
        pd.DataFrame({"id": [1], "name": ["Ana"]}),
        "customers",
        "customers.csv",
    )

    result = analysis_service.analyze(
        pd.DataFrame({"id": [2]}),
        "customers",
        "customers.csv",
    )

    assert [(issue.code, issue.column) for issue in result.schema_drift_issues] == [
        ("COLUMN_REMOVED", "name")
    ]


def test_changed_column_type_is_reported_as_schema_drift(analysis_setup):
    _, _, analysis_service = analysis_setup
    analysis_service.analyze(
        pd.DataFrame({"id": pd.Series([1], dtype="int64")}),
        "customers",
        "customers.csv",
    )

    result = analysis_service.analyze(
        pd.DataFrame({"id": pd.Series(["one"], dtype="object")}),
        "customers",
        "customers.csv",
    )

    assert [(issue.code, issue.column) for issue in result.schema_drift_issues] == [
        ("COLUMN_TYPE_CHANGED", "id")
    ]


def test_schema_drift_does_not_update_the_baseline(analysis_setup):
    repository, _, analysis_service = analysis_setup
    initial = analysis_service.analyze(
        pd.DataFrame({"id": [1]}),
        "customers",
        "customers.csv",
    )

    changed = analysis_service.analyze(
        pd.DataFrame({"id": [2], "email": ["ana@example.com"]}),
        "customers",
        "customers.csv",
    )

    current = repository.get_current("customers")
    assert initial.baseline_version == changed.baseline_version == 1
    assert changed.baseline_created is False
    assert current is not None
    assert current.version == 1
    assert current.schema == {"id": "int64"}


def test_datasets_have_independent_baselines(analysis_setup):
    repository, _, analysis_service = analysis_setup

    customers = analysis_service.analyze(
        pd.DataFrame({"customer_id": [1]}),
        "customers",
        "customers.csv",
    )
    orders = analysis_service.analyze(
        pd.DataFrame({"order_id": [1]}),
        "orders",
        "orders.csv",
    )

    assert customers.dataset_id != orders.dataset_id
    assert customers.baseline_created is True
    assert orders.baseline_created is True
    customer_baseline = repository.get_current("customers")
    order_baseline = repository.get_current("orders")
    assert customer_baseline is not None
    assert order_baseline is not None
    assert customer_baseline.schema == {"customer_id": "int64"}
    assert order_baseline.schema == {"order_id": "int64"}


def test_quality_issues_are_preserved_separately_from_schema_drift(
    analysis_setup,
):
    _, _, analysis_service = analysis_setup
    analysis_service.analyze(
        pd.DataFrame({"id": [1, 2]}),
        "customers",
        "customers.csv",
    )

    result = analysis_service.analyze(
        pd.DataFrame({"id": [3, None], "email": ["a@example.com", None]}),
        "customers",
        "customers.csv",
    )

    quality_issue_codes = {issue.code for issue in result.quality_report.issues}
    drift_issue_codes = {issue.code for issue in result.schema_drift_issues}

    assert "MISSING_VALUES" in quality_issue_codes
    assert "COLUMN_ADDED" in drift_issue_codes
    assert result.quality_report.dataset_name == "customers.csv"


def test_filename_change_does_not_create_a_new_dataset(analysis_setup):
    repository, _, analysis_service = analysis_setup
    first = analysis_service.analyze(
        pd.DataFrame({"id": [1]}),
        "customers",
        "upload-2026-01.csv",
    )

    second = analysis_service.analyze(
        pd.DataFrame({"id": [2]}),
        "customers",
        "renamed-customers.csv",
    )

    assert first.baseline_created is True
    assert second.baseline_created is False
    assert second.dataset_id == "customers"
    assert second.quality_report.dataset_name == "renamed-customers.csv"
    assert len(repository.list_versions("customers")) == 1


def test_schema_registry_service_exposes_approval_and_history(analysis_setup):
    _, registry_service, _ = analysis_setup
    initial, created = registry_service.get_or_create_baseline(
        "customers",
        {"id": "int64"},
    )

    approved = registry_service.approve_schema(
        "customers",
        {"id": "int64", "name": "object"},
        expected_version=initial.version,
    )

    assert created is True
    assert approved.version == 2
    assert registry_service.list_versions("customers") == [initial, approved]
