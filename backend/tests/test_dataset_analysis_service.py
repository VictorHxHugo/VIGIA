import sqlite3
from collections.abc import Sequence
from typing import cast
from uuid import UUID, uuid4

import numpy as np
import pandas as pd
import pytest

import app.datasets.service as dataset_service_module
from app.core.analysis_limits import AnalysisDimensionExceededError
from app.datasets.sqlite_repository import SQLiteDatasetAnalysisRepository
from app.datasets.service import DatasetAnalysisService
from app.incidents import (
    IncidentFilters,
    IncidentProcessingResult,
    IncidentService,
    SQLiteIncidentRepository,
)
from app.quality.schemas import QualityIssue
from app.schema_registry.service import SchemaRegistryService
from app.schema_registry.sqlite_repository import SQLiteSchemaRegistryRepository


@pytest.fixture
def analysis_setup(tmp_path):
    database_path = tmp_path / "registry.sqlite3"
    repository = SQLiteSchemaRegistryRepository(database_path)
    incident_repository = SQLiteIncidentRepository(database_path)
    incident_service = IncidentService(incident_repository)
    registry_service = SchemaRegistryService(repository)
    analysis_repository = SQLiteDatasetAnalysisRepository(
        database_path,
        repository,
        incident_repository,
    )
    analysis_service = DatasetAnalysisService(
        registry_service,
        incident_service,
        analysis_repository,
    )
    return repository, registry_service, analysis_service, incident_repository


def test_first_analysis_creates_version_one_without_drift(analysis_setup):
    repository, _, analysis_service, _ = analysis_setup

    result = analysis_service.analyze(
        pd.DataFrame({"id": [1, 2]}),
        dataset_id="customers",
        dataset_name="customers.csv",
        analysis_id="analysis-1",
    )

    assert result.dataset_id == "customers"
    assert result.baseline_version == 1
    assert result.baseline_created is True
    assert result.schema_drift_issues == []
    current = repository.get_current("customers")
    assert current is not None
    assert current.version == 1


def test_identical_followup_schema_has_no_drift(analysis_setup):
    _, _, analysis_service, _ = analysis_setup
    first = analysis_service.analyze(
        pd.DataFrame({"id": [1, 2]}),
        "customers",
        "customers.csv",
        analysis_id="analysis-1",
    )

    second = analysis_service.analyze(
        pd.DataFrame({"id": [3, 4]}),
        "customers",
        "customers-next.csv",
        analysis_id="analysis-2",
    )

    assert first.baseline_created is True
    assert second.baseline_created is False
    assert second.baseline_version == 1
    assert second.schema_drift_issues == []


def test_added_column_is_reported_as_schema_drift(analysis_setup):
    _, _, analysis_service, _ = analysis_setup
    analysis_service.analyze(
        pd.DataFrame({"id": [1]}),
        "customers",
        "customers.csv",
        analysis_id="analysis-1",
    )

    result = analysis_service.analyze(
        pd.DataFrame({"id": [2], "email": ["ana@example.com"]}),
        "customers",
        "customers.csv",
        analysis_id="analysis-2",
    )

    assert [(issue.code, issue.column) for issue in result.schema_drift_issues] == [
        ("COLUMN_ADDED", "email")
    ]


def test_removed_column_is_reported_as_schema_drift(analysis_setup):
    _, _, analysis_service, _ = analysis_setup
    analysis_service.analyze(
        pd.DataFrame({"id": [1], "name": ["Ana"]}),
        "customers",
        "customers.csv",
        analysis_id="analysis-1",
    )

    result = analysis_service.analyze(
        pd.DataFrame({"id": [2]}),
        "customers",
        "customers.csv",
        analysis_id="analysis-2",
    )

    assert [(issue.code, issue.column) for issue in result.schema_drift_issues] == [
        ("COLUMN_REMOVED", "name")
    ]


def test_changed_column_type_is_reported_as_schema_drift(analysis_setup):
    _, _, analysis_service, _ = analysis_setup
    analysis_service.analyze(
        pd.DataFrame({"id": pd.Series([1], dtype="int64")}),
        "customers",
        "customers.csv",
        analysis_id="analysis-1",
    )

    result = analysis_service.analyze(
        pd.DataFrame({"id": pd.Series(["one"], dtype="object")}),
        "customers",
        "customers.csv",
        analysis_id="analysis-2",
    )

    assert [(issue.code, issue.column) for issue in result.schema_drift_issues] == [
        ("COLUMN_TYPE_CHANGED", "id")
    ]


def test_schema_drift_does_not_update_the_baseline(analysis_setup):
    repository, _, analysis_service, _ = analysis_setup
    initial = analysis_service.analyze(
        pd.DataFrame({"id": [1]}),
        "customers",
        "customers.csv",
        analysis_id="analysis-1",
    )

    changed = analysis_service.analyze(
        pd.DataFrame({"id": [2], "email": ["ana@example.com"]}),
        "customers",
        "customers.csv",
        analysis_id="analysis-2",
    )

    current = repository.get_current("customers")
    assert initial.baseline_version == changed.baseline_version == 1
    assert changed.baseline_created is False
    assert current is not None
    assert current.version == 1
    assert current.schema == {"id": "int64"}


def test_datasets_have_independent_baselines(analysis_setup):
    repository, _, analysis_service, _ = analysis_setup

    customers = analysis_service.analyze(
        pd.DataFrame({"customer_id": [1]}),
        "customers",
        "customers.csv",
        analysis_id="analysis-customers",
    )
    orders = analysis_service.analyze(
        pd.DataFrame({"order_id": [1]}),
        "orders",
        "orders.csv",
        analysis_id="analysis-orders",
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
    _, _, analysis_service, incident_repository = analysis_setup
    analysis_service.analyze(
        pd.DataFrame({"id": [1, 2]}),
        "customers",
        "customers.csv",
        analysis_id="analysis-1",
    )

    result = analysis_service.analyze(
        pd.DataFrame({"id": [3, None], "email": ["a@example.com", None]}),
        "customers",
        "customers.csv",
        analysis_id="analysis-2",
    )

    quality_issue_codes = {issue.code for issue in result.quality_report.issues}
    drift_issue_codes = {issue.code for issue in result.schema_drift_issues}

    assert "MISSING_VALUES" in quality_issue_codes
    assert "COLUMN_ADDED" in drift_issue_codes
    assert result.quality_report.dataset_name == "customers.csv"
    incident_codes = {
        incident.code
        for incident in incident_repository.list_incidents(
            IncidentFilters(dataset_id="customers")
        )
    }
    assert quality_issue_codes <= incident_codes
    assert drift_issue_codes <= incident_codes


def test_filename_change_does_not_create_a_new_dataset(analysis_setup):
    repository, _, analysis_service, _ = analysis_setup
    first = analysis_service.analyze(
        pd.DataFrame({"id": [1]}),
        "customers",
        "upload-2026-01.csv",
        analysis_id="analysis-1",
    )

    second = analysis_service.analyze(
        pd.DataFrame({"id": [2]}),
        "customers",
        "renamed-customers.csv",
        analysis_id="analysis-2",
    )

    assert first.baseline_created is True
    assert second.baseline_created is False
    assert second.dataset_id == "customers"
    assert second.quality_report.dataset_name == "renamed-customers.csv"
    assert len(repository.list_versions("customers")) == 1


def test_schema_registry_service_exposes_approval_and_history(analysis_setup):
    _, registry_service, _, _ = analysis_setup
    initial, created = registry_service.get_or_create_baseline(
        "customers",
        {"id": "int64"},
    )

    approved_by_user_id = uuid4()
    approved = registry_service.approve_schema(
        "customers",
        {"id": "int64", "name": "object"},
        expected_version=initial.version,
        approved_by_user_id=approved_by_user_id,
    )

    assert created is True
    assert approved.version == 2
    assert approved.approved_by_user_id == approved_by_user_id
    assert registry_service.list_versions("customers") == [initial, approved]


def test_schema_registry_service_rejects_missing_approver(analysis_setup):
    _, registry_service, _, _ = analysis_setup
    initial, _ = registry_service.get_or_create_baseline(
        "customers",
        {"id": "int64"},
    )

    with pytest.raises(ValueError, match="approved_by_user_id must be a UUID"):
        registry_service.approve_schema(
            "customers",
            {"id": "int64", "name": "object"},
            expected_version=initial.version,
            approved_by_user_id=cast(UUID, None),
        )

    assert registry_service.list_versions("customers") == [initial]


class RecordingIncidentService(IncidentService):
    def __init__(
        self,
        result: IncidentProcessingResult | None = None,
        error: Exception | None = None,
    ) -> None:
        self.result = result or IncidentProcessingResult()
        self.error = error
        self.calls: list[tuple[str, str, list[QualityIssue]]] = []

    def process_issues_in(
        self,
        unit_of_work,
        dataset_id: str,
        analysis_id: str,
        issues: Sequence[QualityIssue],
        *,
        detected_at=None,
    ) -> IncidentProcessingResult:
        self.calls.append((dataset_id, analysis_id, list(issues)))
        if self.error is not None:
            raise self.error
        return self.result


def make_analysis_service(
    schema_repository,
    incident_repository,
    incident_service,
):
    analysis_repository = SQLiteDatasetAnalysisRepository(
        schema_repository.database_path,
        schema_repository,
        incident_repository,
        busy_timeout_ms=schema_repository.busy_timeout_ms,
    )
    return DatasetAnalysisService(
        SchemaRegistryService(schema_repository),
        incident_service,
        analysis_repository,
    )


def test_initial_analysis_forwards_quality_issues_and_analysis_id(tmp_path):
    database_path = tmp_path / "registry.sqlite3"
    repository = SQLiteSchemaRegistryRepository(database_path)
    incident_repository = SQLiteIncidentRepository(database_path)
    incident_service = RecordingIncidentService(
        IncidentProcessingResult(created_count=1),
    )
    analysis_service = make_analysis_service(
        repository,
        incident_repository,
        incident_service,
    )

    result = analysis_service.analyze(
        pd.DataFrame({"id": [1, None]}),
        "customers",
        "customers.csv",
        analysis_id="stable-analysis-id",
    )

    dataset_id, analysis_id, issues = incident_service.calls[0]
    assert dataset_id == "customers"
    assert analysis_id == "stable-analysis-id"
    assert result.quality_report.issues
    assert issues == result.quality_report.issues
    assert result.incident_processing == IncidentProcessingResult(created_count=1)


def test_later_analysis_forwards_quality_and_schema_drift_without_field_loss(
    tmp_path,
):
    database_path = tmp_path / "registry.sqlite3"
    repository = SQLiteSchemaRegistryRepository(database_path)
    incident_repository = SQLiteIncidentRepository(database_path)
    incident_service = RecordingIncidentService()
    analysis_service = make_analysis_service(
        repository,
        incident_repository,
        incident_service,
    )
    analysis_service.analyze(
        pd.DataFrame({"id": [1, 2]}),
        "customers",
        "customers.csv",
        analysis_id="analysis-1",
    )
    incident_service.calls.clear()

    result = analysis_service.analyze(
        pd.DataFrame({"id": [3, None], "email": ["a@example.com", None]}),
        "customers",
        "customers.csv",
        analysis_id="analysis-2",
    )

    _, forwarded_analysis_id, combined_issues = incident_service.calls[0]
    assert forwarded_analysis_id == "analysis-2"
    assert result.quality_report.issues
    assert result.schema_drift_issues
    assert combined_issues == [
        *result.quality_report.issues,
        *result.schema_drift_issues,
    ]
    assert combined_issues[0] is result.quality_report.issues[0]
    forwarded_schema_drift_issues = combined_issues[
        len(result.quality_report.issues) :
    ]
    assert len(forwarded_schema_drift_issues) == len(result.schema_drift_issues)
    assert all(
        forwarded is expected
        for forwarded, expected in zip(
            forwarded_schema_drift_issues,
            result.schema_drift_issues,
        )
    )
    assert combined_issues[0].model_dump() == result.quality_report.issues[0].model_dump()
    assert all(
        forwarded.model_dump() == expected.model_dump()
        for forwarded, expected in zip(
            forwarded_schema_drift_issues,
            result.schema_drift_issues,
        )
    )


def test_analysis_exposes_incident_processing_counts(tmp_path):
    database_path = tmp_path / "registry.sqlite3"
    repository = SQLiteSchemaRegistryRepository(database_path)
    incident_repository = SQLiteIncidentRepository(database_path)
    processing_result = IncidentProcessingResult(
        created_count=2,
        updated_count=3,
        reopened_count=1,
        ignored_occurrence_count=4,
    )
    incident_service = RecordingIncidentService(processing_result)
    analysis_service = make_analysis_service(
        repository,
        incident_repository,
        incident_service,
    )

    result = analysis_service.analyze(
        pd.DataFrame({"id": [1]}),
        "customers",
        "customers.csv",
        analysis_id="analysis-1",
    )

    assert result.incident_processing is processing_result
    assert result.incident_processing.created_count == 2
    assert result.incident_processing.updated_count == 3
    assert result.incident_processing.reopened_count == 1
    assert result.incident_processing.ignored_occurrence_count == 4


def test_incident_service_failure_is_propagated(tmp_path):
    database_path = tmp_path / "registry.sqlite3"
    repository = SQLiteSchemaRegistryRepository(database_path)
    incident_repository = SQLiteIncidentRepository(database_path)
    failure = RuntimeError("incident persistence failed")
    incident_service = RecordingIncidentService(error=failure)
    analysis_service = make_analysis_service(
        repository,
        incident_repository,
        incident_service,
    )

    with pytest.raises(RuntimeError, match="incident persistence failed") as error:
        analysis_service.analyze(
            pd.DataFrame({"id": [1]}),
            "customers",
            "customers.csv",
            analysis_id="analysis-1",
        )

    assert error.value is failure
    assert repository.get_current("customers") is None


def test_analysis_without_issues_calls_incident_service_with_empty_sequence(tmp_path):
    database_path = tmp_path / "registry.sqlite3"
    repository = SQLiteSchemaRegistryRepository(database_path)
    incident_repository = SQLiteIncidentRepository(database_path)
    incident_service = RecordingIncidentService()
    analysis_service = make_analysis_service(
        repository,
        incident_repository,
        incident_service,
    )

    result = analysis_service.analyze(
        pd.DataFrame({"id": [1, 2]}),
        "customers",
        "customers.csv",
        analysis_id="analysis-1",
    )

    assert result.quality_report.issues == []
    assert result.schema_drift_issues == []
    assert incident_service.calls == [("customers", "analysis-1", [])]
    assert result.incident_processing == IncidentProcessingResult()


def test_analysis_uses_shared_uow_and_persists_baseline_and_incidents(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "registry.sqlite3"
    schema_repository = SQLiteSchemaRegistryRepository(database_path)
    incident_repository = SQLiteIncidentRepository(database_path)
    schema_service = SchemaRegistryService(schema_repository)
    incident_service = IncidentService(incident_repository)
    analysis_repository = SQLiteDatasetAnalysisRepository(
        database_path,
        schema_repository,
        incident_repository,
    )
    analysis_service = DatasetAnalysisService(
        schema_service,
        incident_service,
        analysis_repository,
    )
    observed_uows = {}
    original_schema_operation = schema_service.get_or_create_baseline_in
    original_incident_operation = incident_service.process_issues_in

    def record_schema_uow(unit_of_work, dataset_id, schema):
        observed_uows["schema"] = unit_of_work
        return original_schema_operation(unit_of_work, dataset_id, schema)

    def record_incident_uow(
        unit_of_work,
        dataset_id,
        analysis_id,
        issues,
        *,
        detected_at=None,
    ):
        observed_uows["incident"] = unit_of_work
        return original_incident_operation(
            unit_of_work,
            dataset_id,
            analysis_id,
            issues,
            detected_at=detected_at,
        )

    monkeypatch.setattr(
        schema_service,
        "get_or_create_baseline_in",
        record_schema_uow,
    )
    monkeypatch.setattr(
        incident_service,
        "process_issues_in",
        record_incident_uow,
    )

    result = analysis_service.analyze(
        pd.DataFrame({"id": [1, None]}),
        "customers",
        "customers.csv",
        analysis_id="atomic-analysis",
    )

    baseline = schema_repository.get_current("customers")
    incidents = incident_repository.list_incidents(
        IncidentFilters(dataset_id="customers")
    )
    assert baseline is not None
    assert result.baseline_created is True
    assert result.incident_processing.created_count > 0
    assert incidents
    assert observed_uows["schema"]._connection is observed_uows["incident"]._connection
    assert len(incident_repository.list_occurrences(incidents[0].incident_id)) == 1


def test_incident_history_failure_rolls_back_baseline_and_partial_incident_writes(
    tmp_path,
):
    database_path = tmp_path / "registry.sqlite3"
    schema_repository = SQLiteSchemaRegistryRepository(database_path)
    incident_repository = SQLiteIncidentRepository(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_initial_status_change
            BEFORE INSERT ON incident_status_history
            BEGIN
                SELECT RAISE(ABORT, 'forced status history failure');
            END
            """
        )

    analysis_service = make_analysis_service(
        schema_repository,
        incident_repository,
        IncidentService(incident_repository),
    )

    with pytest.raises(sqlite3.IntegrityError, match="forced status history failure"):
        analysis_service.analyze(
            pd.DataFrame({"id": [1, None]}),
            "customers",
            "customers.csv",
            analysis_id="failing-analysis",
        )

    assert schema_repository.get_current("customers") is None
    assert incident_repository.list_incidents(
        IncidentFilters(dataset_id="customers")
    ) == []


@pytest.mark.parametrize(
    ("dimension", "limit", "observed"),
    [
        ("columns", 200, 201),
        ("rows", 100_000, 100_001),
        ("cells", 2_000_000, 2_000_200),
    ],
)
def test_excessive_dataframe_dimensions_fail_before_quality_or_transaction(
    analysis_setup,
    monkeypatch,
    dimension: str,
    limit: int,
    observed: int,
):
    repository, _, analysis_service, incident_repository = analysis_setup
    dataset_id = "oversized-dataset"
    analysis_repository = analysis_service._analysis_repository

    if dimension == "columns":
        df = pd.DataFrame(
            {f"column_{index}": [index] for index in range(observed)}
        )
    elif dimension == "rows":
        df = pd.DataFrame({"value": range(observed)})
    else:
        df = pd.DataFrame(
            np.zeros((10_001, 200), dtype=np.uint8)
        )

    def unexpected_quality_analysis(*args, **kwargs):
        raise AssertionError("Quality analysis ran before dimension validation.")

    def unexpected_transaction():
        raise AssertionError("The aggregate transaction was opened.")

    monkeypatch.setattr(
        dataset_service_module,
        "analyze_dataset",
        unexpected_quality_analysis,
    )
    monkeypatch.setattr(
        analysis_repository,
        "transaction",
        unexpected_transaction,
    )

    with pytest.raises(AnalysisDimensionExceededError) as exc_info:
        analysis_service.analyze(
            df,
            dataset_id,
            "oversized.csv",
            analysis_id="oversized-analysis",
        )

    assert exc_info.value.dimension == dimension
    assert exc_info.value.limit == limit
    assert exc_info.value.observed == observed
    assert repository.get_current(dataset_id) is None
    assert incident_repository.list_incidents(
        IncidentFilters(dataset_id=dataset_id)
    ) == []
    with sqlite3.connect(repository.database_path) as connection:
        occurrence_count = connection.execute(
            "SELECT COUNT(*) FROM incident_occurrences"
        ).fetchone()[0]
    assert occurrence_count == 0
