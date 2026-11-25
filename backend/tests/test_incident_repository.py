import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from threading import Barrier

import pytest

from app.incidents import (
    Incident,
    IncidentAlreadyExistsError,
    IncidentFilters,
    IncidentNotFoundError,
    IncidentOccurrence,
    IncidentRevisionConflictError,
    IncidentSeverity,
    IncidentStatus,
    IncidentStatusChange,
    SQLiteIncidentRepository,
)
from app.schema_registry.sqlite_repository import SQLiteSchemaRegistryRepository


NOW = datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc)


def make_incident(
    *,
    incident_id: str = "incident-1",
    dataset_id: str = "customers",
    fingerprint: str = "fingerprint-1",
    revision: int = 1,
    updated_at: datetime = NOW,
    column: str | None = "email",
    status: IncidentStatus = IncidentStatus.OPEN,
    resolved_at: datetime | None = None,
) -> Incident:
    return Incident(
        incident_id=incident_id,
        dataset_id=dataset_id,
        fingerprint=fingerprint,
        code="NULL_VALUES",
        title="Null values found",
        description="A required field contains null values.",
        column=column,
        severity=IncidentSeverity.HIGH,
        status=status,
        created_at=NOW,
        updated_at=updated_at,
        first_seen_at=NOW - timedelta(days=1),
        last_seen_at=updated_at,
        resolved_at=resolved_at,
        occurrence_count=2,
        revision=revision,
    )


def make_occurrence(
    *,
    occurrence_id: str = "occurrence-1",
    incident_id: str = "incident-1",
    analysis_id: str = "analysis-1",
    detected_at: datetime = NOW,
) -> IncidentOccurrence:
    return IncidentOccurrence(
        occurrence_id=occurrence_id,
        incident_id=incident_id,
        analysis_id=analysis_id,
        detected_at=detected_at,
        code="NULL_VALUES",
        title="Null values found",
        description="A required field contains null values.",
        column="email",
        severity=IncidentSeverity.HIGH,
        affected_rows=3,
        examples=(2, 4, 9),
    )


def make_status_change(
    *,
    change_id: str = "change-1",
    incident_id: str = "incident-1",
    from_status: IncidentStatus | None = IncidentStatus.OPEN,
    to_status: IncidentStatus = IncidentStatus.ACKNOWLEDGED,
) -> IncidentStatusChange:
    return IncidentStatusChange(
        change_id=change_id,
        incident_id=incident_id,
        from_status=from_status,
        to_status=to_status,
        changed_at=NOW,
        actor="operator@example.test",
        reason="Investigating the alert.",
    )


def test_creates_and_reconstructs_incident_with_all_model_fields(tmp_path):
    repository = SQLiteIncidentRepository(tmp_path / "nested" / "incidents.sqlite3")
    incident = make_incident(
        column=None,
        status=IncidentStatus.RESOLVED,
        resolved_at=NOW + timedelta(hours=1),
    )

    with repository.transaction() as unit_of_work:
        assert unit_of_work.create_incident(incident) == incident

    assert repository.get_incident(incident.incident_id) == incident


def test_create_requires_initial_revision_one(tmp_path):
    repository = SQLiteIncidentRepository(tmp_path / "incidents.sqlite3")

    with repository.transaction() as unit_of_work:
        with pytest.raises(ValueError, match="revision 1"):
            unit_of_work.create_incident(make_incident(revision=2))


def test_duplicate_dataset_fingerprint_maps_to_already_exists(tmp_path):
    repository = SQLiteIncidentRepository(tmp_path / "incidents.sqlite3")
    with repository.transaction() as unit_of_work:
        unit_of_work.create_incident(make_incident())

    with repository.transaction() as unit_of_work:
        with pytest.raises(IncidentAlreadyExistsError) as error:
            unit_of_work.create_incident(
                make_incident(incident_id="incident-2")
            )

    assert error.value.dataset_id == "customers"
    assert error.value.fingerprint == "fingerprint-1"


def test_other_create_integrity_errors_are_not_mapped(tmp_path):
    repository = SQLiteIncidentRepository(tmp_path / "incidents.sqlite3")
    with repository.transaction() as unit_of_work:
        unit_of_work.create_incident(make_incident())

    with repository.transaction() as unit_of_work:
        with pytest.raises(sqlite3.IntegrityError):
            unit_of_work.create_incident(
                make_incident(fingerprint="different-fingerprint")
            )


def test_update_increments_revision_and_reconstructs_record(tmp_path):
    repository = SQLiteIncidentRepository(tmp_path / "incidents.sqlite3")
    original = make_incident()
    with repository.transaction() as unit_of_work:
        unit_of_work.create_incident(original)

    replacement = replace(
        original,
        status=IncidentStatus.ACKNOWLEDGED,
        updated_at=NOW + timedelta(minutes=5),
        revision=1,
    )
    with repository.transaction() as unit_of_work:
        updated = unit_of_work.update_incident(
            replacement,
            expected_revision=1,
        )

    assert updated.revision == 2
    assert updated.status is IncidentStatus.ACKNOWLEDGED
    assert updated.updated_at == NOW + timedelta(minutes=5)
    assert repository.get_incident(original.incident_id) == updated


def test_update_rejects_candidate_revision_not_equal_to_expected(tmp_path):
    repository = SQLiteIncidentRepository(tmp_path / "incidents.sqlite3")
    with repository.transaction() as unit_of_work:
        unit_of_work.create_incident(make_incident())

    with repository.transaction() as unit_of_work:
        with pytest.raises(ValueError, match="incident.revision"):
            unit_of_work.update_incident(
                make_incident(revision=2),
                expected_revision=1,
            )


def test_stale_update_raises_revision_conflict_without_overwriting(tmp_path):
    repository = SQLiteIncidentRepository(tmp_path / "incidents.sqlite3")
    original = make_incident()
    with repository.transaction() as unit_of_work:
        unit_of_work.create_incident(original)

    with repository.transaction() as unit_of_work:
        unit_of_work.update_incident(
            replace(original, status=IncidentStatus.ACKNOWLEDGED),
            expected_revision=1,
        )

    with repository.transaction() as unit_of_work:
        with pytest.raises(IncidentRevisionConflictError) as error:
            unit_of_work.update_incident(
                replace(original, status=IncidentStatus.RESOLVED),
                expected_revision=1,
            )

    assert error.value.expected_revision == 1
    assert error.value.current_revision == 2
    assert repository.get_incident(original.incident_id).status is (
        IncidentStatus.ACKNOWLEDGED
    )


def test_update_of_missing_incident_raises_not_found(tmp_path):
    repository = SQLiteIncidentRepository(tmp_path / "incidents.sqlite3")

    with repository.transaction() as unit_of_work:
        with pytest.raises(IncidentNotFoundError):
            unit_of_work.update_incident(
                make_incident(),
                expected_revision=1,
            )


def test_transaction_persists_occurrences_and_status_history(tmp_path):
    repository = SQLiteIncidentRepository(tmp_path / "incidents.sqlite3")
    incident = make_incident()
    occurrence = make_occurrence()
    initial_change = make_status_change(
        change_id="change-initial",
        from_status=None,
        to_status=IncidentStatus.OPEN,
    )
    later_change = make_status_change(
        change_id="change-acknowledged",
        from_status=IncidentStatus.OPEN,
        to_status=IncidentStatus.ACKNOWLEDGED,
    )

    with repository.transaction() as unit_of_work:
        unit_of_work.create_incident(incident)
        assert unit_of_work.record_occurrence(occurrence) is True
        unit_of_work.record_status_change(initial_change)
        unit_of_work.record_status_change(later_change)

    assert repository.list_occurrences(incident.incident_id) == [occurrence]
    assert repository.list_status_history(incident.incident_id) == [
        initial_change,
        later_change,
    ]


def test_record_occurrence_returns_false_only_for_duplicate_analysis(tmp_path):
    repository = SQLiteIncidentRepository(tmp_path / "incidents.sqlite3")
    with repository.transaction() as unit_of_work:
        unit_of_work.create_incident(make_incident())
        assert unit_of_work.record_occurrence(make_occurrence()) is True

    with repository.transaction() as unit_of_work:
        duplicate_analysis = make_occurrence(occurrence_id="occurrence-2")
        assert unit_of_work.record_occurrence(duplicate_analysis) is False

    with repository.transaction() as unit_of_work:
        with pytest.raises(sqlite3.IntegrityError):
            unit_of_work.record_occurrence(
                make_occurrence(
                    occurrence_id="occurrence-1",
                    analysis_id="analysis-2",
                )
            )


def test_foreign_key_errors_are_propagated(tmp_path):
    repository = SQLiteIncidentRepository(tmp_path / "incidents.sqlite3")

    with repository.transaction() as unit_of_work:
        with pytest.raises(sqlite3.IntegrityError):
            unit_of_work.record_occurrence(
                make_occurrence(incident_id="missing-incident")
            )

    with repository.transaction() as unit_of_work:
        with pytest.raises(sqlite3.IntegrityError):
            unit_of_work.record_status_change(
                make_status_change(incident_id="missing-incident")
            )


def test_exception_rolls_back_incident_occurrence_and_status_change(tmp_path):
    repository = SQLiteIncidentRepository(tmp_path / "incidents.sqlite3")

    with pytest.raises(RuntimeError, match="abort unit of work"):
        with repository.transaction() as unit_of_work:
            unit_of_work.create_incident(make_incident())
            unit_of_work.record_occurrence(make_occurrence())
            unit_of_work.record_status_change(make_status_change())
            raise RuntimeError("abort unit of work")

    assert repository.get_incident("incident-1") is None
    assert repository.list_occurrences("incident-1") == []
    assert repository.list_status_history("incident-1") == []


def test_database_constraints_enforce_unique_values_checks_and_foreign_keys(
    tmp_path,
):
    database_path = tmp_path / "incidents.sqlite3"
    repository = SQLiteIncidentRepository(database_path)
    with repository.transaction() as unit_of_work:
        unit_of_work.create_incident(make_incident())

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO incidents (
                    incident_id, dataset_id, fingerprint, code, title,
                    description, severity, status, created_at, updated_at,
                    first_seen_at, last_seen_at, occurrence_count, revision
                )
                VALUES (
                    'incident-2', 'customers', 'fingerprint-1', 'X', 'T',
                    'D', 'high', 'OPEN', 'now', 'now', 'now', 'now', 0, 1
                )
                """
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO incidents (
                    incident_id, dataset_id, fingerprint, code, title,
                    description, severity, status, created_at, updated_at,
                    first_seen_at, last_seen_at, occurrence_count, revision
                )
                VALUES (
                    'incident-invalid', 'other', 'fp', 'X', 'T', 'D',
                    'urgent', 'OPEN', 'now', 'now', 'now', 'now', 0, 1
                )
                """
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO incident_occurrences (
                    occurrence_id, incident_id, analysis_id, detected_at,
                    code, title, description, severity, affected_rows,
                    examples_json
                )
                VALUES (
                    'orphan-occurrence', 'missing', 'analysis', 'now',
                    'X', 'T', 'D', 'high', 0, '[]'
                )
                """
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO incident_occurrences (
                    occurrence_id, incident_id, analysis_id, detected_at,
                    code, title, description, severity, affected_rows,
                    examples_json
                )
                VALUES (
                    'invalid-examples', 'incident-1', 'analysis', 'now',
                    'X', 'T', 'D', 'high', 0, '{invalid'
                )
                """
            )


def test_list_incidents_filters_orders_and_paginates_deterministically(tmp_path):
    repository = SQLiteIncidentRepository(tmp_path / "incidents.sqlite3")
    incidents = [
        make_incident(incident_id="a", dataset_id="customers", updated_at=NOW),
        make_incident(
            incident_id="b",
            dataset_id="customers",
            fingerprint="fingerprint-2",
            updated_at=NOW,
        ),
        make_incident(
            incident_id="c",
            dataset_id="orders",
            fingerprint="fingerprint-3",
            updated_at=NOW + timedelta(minutes=1),
        ),
    ]
    with repository.transaction() as unit_of_work:
        for incident in incidents:
            unit_of_work.create_incident(incident)

    assert [
        item.incident_id
        for item in repository.list_incidents(
            IncidentFilters(dataset_id="customers")
        )
    ] == ["a", "b"]
    assert [
        item.incident_id
        for item in repository.list_incidents(IncidentFilters(limit=1))
    ] == ["c"]
    assert [
        item.incident_id
        for item in repository.list_incidents(
            IncidentFilters(limit=1, offset=1)
        )
    ] == ["a"]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"limit": 0}, "limit"),
        ({"limit": 101}, "limit"),
        ({"limit": True}, "limit"),
        ({"offset": -1}, "offset"),
        ({"offset": False}, "offset"),
        ({"status": "NOT_A_STATUS"}, "status"),
        ({"severity": "urgent"}, "severity"),
    ],
)
def test_incident_filters_validate_values(kwargs, message):
    with pytest.raises(ValueError, match=message):
        IncidentFilters(**kwargs)


@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("list_occurrences", ("incident-1",)),
        ("list_status_history", ("incident-1",)),
    ],
)
def test_history_queries_validate_pagination(tmp_path, method, args):
    repository = SQLiteIncidentRepository(tmp_path / "incidents.sqlite3")

    with pytest.raises(ValueError, match="limit"):
        getattr(repository, method)(*args, limit=0)


def test_list_occurrences_paginates_with_stable_tie_breaker(tmp_path):
    repository = SQLiteIncidentRepository(tmp_path / "incidents.sqlite3")
    with repository.transaction() as unit_of_work:
        unit_of_work.create_incident(make_incident())
        unit_of_work.record_occurrence(
            make_occurrence(
                occurrence_id="occurrence-b",
                analysis_id="analysis-b",
            )
        )
        unit_of_work.record_occurrence(
            make_occurrence(
                occurrence_id="occurrence-a",
                analysis_id="analysis-a",
            )
        )

    assert [
        occurrence.occurrence_id
        for occurrence in repository.list_occurrences(
            "incident-1",
            limit=1,
        )
    ] == ["occurrence-a"]
    assert [
        occurrence.occurrence_id
        for occurrence in repository.list_occurrences(
            "incident-1",
            limit=1,
            offset=1,
        )
    ] == ["occurrence-b"]


def test_persists_after_reopening_repository(tmp_path):
    database_path = tmp_path / "incidents.sqlite3"
    first_repository = SQLiteIncidentRepository(database_path)
    incident = make_incident()
    occurrence = make_occurrence()
    change = make_status_change()
    with first_repository.transaction() as unit_of_work:
        unit_of_work.create_incident(incident)
        unit_of_work.record_occurrence(occurrence)
        unit_of_work.record_status_change(change)

    reopened_repository = SQLiteIncidentRepository(database_path)

    assert reopened_repository.get_incident(incident.incident_id) == incident
    assert reopened_repository.list_occurrences(incident.incident_id) == [occurrence]
    assert reopened_repository.list_status_history(incident.incident_id) == [change]


def test_coexists_with_schema_registry_in_same_database_file(tmp_path):
    database_path = tmp_path / "shared.sqlite3"
    schema_repository = SQLiteSchemaRegistryRepository(database_path)
    incident_repository = SQLiteIncidentRepository(database_path)
    expected_schema, created = schema_repository.get_or_create_initial(
        "customers",
        {"id": "int64"},
    )
    with incident_repository.transaction() as unit_of_work:
        unit_of_work.create_incident(make_incident())

    reopened_schema_repository = SQLiteSchemaRegistryRepository(database_path)
    reopened_incident_repository = SQLiteIncidentRepository(database_path)

    assert created is True
    assert reopened_schema_repository.get_current("customers") == expected_schema
    assert reopened_incident_repository.get_incident("incident-1") == make_incident()
    with sqlite3.connect(database_path) as connection:
        table_names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = ?",
                ("table",),
            )
        }
    assert "schema_versions" in table_names
    assert {
        "incidents",
        "incident_occurrences",
        "incident_status_history",
    }.issubset(table_names)


def test_concurrent_fingerprint_creation_keeps_one_incident(tmp_path):
    database_path = tmp_path / "incidents.sqlite3"
    repositories = [
        SQLiteIncidentRepository(database_path, busy_timeout_ms=10000)
        for _ in range(2)
    ]
    barrier = Barrier(2)

    def create_if_missing(repository, incident_id):
        barrier.wait()
        with repository.transaction() as unit_of_work:
            current = unit_of_work.get_by_fingerprint(
                "customers",
                "fingerprint-1",
            )
            if current is not None:
                return False
            unit_of_work.create_incident(make_incident(incident_id=incident_id))
            return True

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(create_if_missing, repositories[0], "incident-a"),
            executor.submit(create_if_missing, repositories[1], "incident-b"),
        ]
        results = [future.result() for future in futures]

    assert sorted(results) == [False, True]
    assert len(
        repositories[0].list_incidents(IncidentFilters(dataset_id="customers"))
    ) == 1


def test_concurrent_updates_only_one_expected_revision_succeeds(tmp_path):
    database_path = tmp_path / "incidents.sqlite3"
    repositories = [
        SQLiteIncidentRepository(database_path, busy_timeout_ms=10000)
        for _ in range(2)
    ]
    original = make_incident()
    with repositories[0].transaction() as unit_of_work:
        unit_of_work.create_incident(original)
    barrier = Barrier(2)

    def update(repository, status):
        barrier.wait()
        try:
            with repository.transaction() as unit_of_work:
                return unit_of_work.update_incident(
                    replace(original, status=status),
                    expected_revision=1,
                )
        except IncidentRevisionConflictError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(update, repositories[0], IncidentStatus.ACKNOWLEDGED),
            executor.submit(update, repositories[1], IncidentStatus.RESOLVED),
        ]
        results = [future.result() for future in futures]

    assert sum(isinstance(result, Incident) for result in results) == 1
    conflict = next(
        result
        for result in results
        if isinstance(result, IncidentRevisionConflictError)
    )
    assert conflict.current_revision == 2
    assert repositories[0].get_incident("incident-1").revision == 2


class FailingConnection:
    def __init__(
        self,
        *,
        commit_error: BaseException | None = None,
        rollback_error: BaseException | None = None,
    ) -> None:
        self.commit_error = commit_error
        self.rollback_error = rollback_error
        self.in_transaction = False
        self.closed = False
        self.rollback_called = False

    def execute(self, statement, parameters=()):
        if statement == "BEGIN IMMEDIATE":
            self.in_transaction = True
        return self

    def commit(self):
        if self.commit_error is not None:
            raise self.commit_error
        self.in_transaction = False

    def rollback(self):
        self.rollback_called = True
        if self.rollback_error is not None:
            raise self.rollback_error
        self.in_transaction = False

    def close(self):
        self.closed = True


def test_connection_closes_when_commit_and_rollback_fail(tmp_path, monkeypatch):
    repository = SQLiteIncidentRepository(tmp_path / "incidents.sqlite3")
    connection = FailingConnection(
        commit_error=sqlite3.OperationalError("commit failed"),
        rollback_error=sqlite3.OperationalError("rollback failed"),
    )
    monkeypatch.setattr(repository, "_open_connection", lambda: connection)

    with pytest.raises(sqlite3.OperationalError, match="rollback failed"):
        with repository.transaction():
            pass

    assert connection.rollback_called is True
    assert connection.closed is True


def test_connection_closes_when_rollback_fails_after_body_exception(
    tmp_path,
    monkeypatch,
):
    repository = SQLiteIncidentRepository(tmp_path / "incidents.sqlite3")
    connection = FailingConnection(
        rollback_error=sqlite3.OperationalError("rollback failed"),
    )
    monkeypatch.setattr(repository, "_open_connection", lambda: connection)

    with pytest.raises(sqlite3.OperationalError, match="rollback failed"):
        with repository.transaction():
            raise RuntimeError("body failed")

    assert connection.rollback_called is True
    assert connection.closed is True


def test_values_are_bound_instead_of_treated_as_sql(tmp_path):
    repository = SQLiteIncidentRepository(tmp_path / "incidents.sqlite3")
    injection_like_id = "' OR 1=1 --"
    with repository.transaction() as unit_of_work:
        unit_of_work.create_incident(
            make_incident(
                incident_id="safe-id",
                dataset_id="safe-dataset",
                fingerprint="safe-fingerprint",
            )
        )

    assert repository.get_incident(injection_like_id) is None
    assert repository.list_incidents(
        IncidentFilters(dataset_id=injection_like_id)
    ) == []
