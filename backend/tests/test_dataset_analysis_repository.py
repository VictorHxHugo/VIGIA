import sqlite3
from datetime import datetime, timezone
from typing import cast

import pytest

from app.datasets.sqlite_repository import SQLiteDatasetAnalysisRepository
from app.incidents.models import (
    Incident,
    IncidentOccurrence,
    IncidentSeverity,
    IncidentStatus,
    IncidentStatusChange,
)
from app.incidents.sqlite_repository import (
    SQLiteIncidentRepository,
    SQLiteIncidentUnitOfWork,
)
from app.schema_registry.sqlite_repository import (
    SQLiteSchemaRegistryRepository,
    SQLiteSchemaRegistryUnitOfWork,
)


def make_repositories(
    database_path,
    *,
    schema_timeout_ms=5000,
    incident_timeout_ms=5000,
):
    schema_repository = SQLiteSchemaRegistryRepository(
        database_path,
        busy_timeout_ms=schema_timeout_ms,
    )
    incident_repository = SQLiteIncidentRepository(
        database_path,
        busy_timeout_ms=incident_timeout_ms,
    )
    return schema_repository, incident_repository


def make_aggregate(database_path, *, busy_timeout_ms=5000):
    schema_repository, incident_repository = make_repositories(database_path)
    repository = SQLiteDatasetAnalysisRepository(
        database_path,
        schema_repository,
        incident_repository,
        busy_timeout_ms=busy_timeout_ms,
    )
    return repository, schema_repository, incident_repository


def write_schema_and_incident(unit_of_work):
    baseline, created = unit_of_work.schemas.get_or_create_initial(
        "customers",
        {"email": "string"},
    )
    assert baseline.version == 1
    assert created

    now = datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc)
    incident = Incident(
        incident_id="incident-1",
        dataset_id="customers",
        fingerprint="fingerprint-1",
        code="NULL_VALUES",
        title="Null values found",
        description="A required field contains null values.",
        column="email",
        severity=IncidentSeverity.HIGH,
        status=IncidentStatus.OPEN,
        created_at=now,
        updated_at=now,
        first_seen_at=now,
        last_seen_at=now,
    )
    unit_of_work.incidents.create_incident(incident)
    unit_of_work.incidents.record_occurrence(
        IncidentOccurrence(
            occurrence_id="occurrence-1",
            incident_id=incident.incident_id,
            analysis_id="analysis-1",
            detected_at=now,
            code=incident.code,
            title=incident.title,
            description=incident.description,
            column=incident.column,
            severity=incident.severity,
            affected_rows=2,
            examples=(1, 3),
        )
    )
    unit_of_work.incidents.record_status_change(
        IncidentStatusChange(
            change_id="change-1",
            incident_id=incident.incident_id,
            from_status=IncidentStatus.OPEN,
            to_status=IncidentStatus.ACKNOWLEDGED,
            changed_at=now,
            actor="operator@example.test",
            reason="Investigating the alert.",
        )
    )


def test_transaction_shares_connection_and_configures_sqlite(tmp_path):
    repository, _, _ = make_aggregate(tmp_path / "analysis.sqlite3")

    with repository.transaction() as unit_of_work:
        schema_unit_of_work = cast(
            SQLiteSchemaRegistryUnitOfWork,
            unit_of_work.schemas,
        )
        incident_unit_of_work = cast(
            SQLiteIncidentUnitOfWork,
            unit_of_work.incidents,
        )
        schema_connection = schema_unit_of_work._connection
        incident_connection = incident_unit_of_work._connection

        assert schema_connection is incident_connection
        assert schema_connection.row_factory is sqlite3.Row
        assert schema_connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert schema_connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert schema_connection.in_transaction

    with pytest.raises(sqlite3.ProgrammingError):
        schema_connection.execute("SELECT 1")


def test_commit_persists_schema_incident_occurrence_and_history(tmp_path):
    repository, schema_repository, incident_repository = make_aggregate(
        tmp_path / "analysis.sqlite3"
    )

    with repository.transaction() as unit_of_work:
        write_schema_and_incident(unit_of_work)

    baseline = schema_repository.get_current("customers")
    assert baseline is not None
    assert baseline.schema == {"email": "string"}
    assert incident_repository.get_incident("incident-1") is not None
    assert len(incident_repository.list_occurrences("incident-1")) == 1
    assert len(incident_repository.list_status_history("incident-1")) == 1


def test_exception_rolls_back_schema_incident_occurrence_and_history(tmp_path):
    repository, schema_repository, incident_repository = make_aggregate(
        tmp_path / "analysis.sqlite3"
    )

    with pytest.raises(RuntimeError, match="forced analysis failure"):
        with repository.transaction() as unit_of_work:
            write_schema_and_incident(unit_of_work)
            raise RuntimeError("forced analysis failure")

    assert schema_repository.get_current("customers") is None
    assert incident_repository.get_incident("incident-1") is None
    assert incident_repository.list_occurrences("incident-1") == []
    assert incident_repository.list_status_history("incident-1") == []


def test_body_failure_attempts_rollback_and_closes_connection(
    monkeypatch,
    tmp_path,
):
    connection = FakeConnection()
    repository = make_aggregate(tmp_path / "analysis.sqlite3")[0]
    monkeypatch.setattr(
        "app.datasets.sqlite_repository.sqlite3.connect",
        lambda *args, **kwargs: connection,
    )
    original_error = RuntimeError("analysis body failed")

    with pytest.raises(RuntimeError, match="analysis body failed") as raised:
        with repository.transaction():
            raise original_error

    assert raised.value is original_error
    assert connection.rollback_called
    assert connection.closed


def test_commit_failure_attempts_rollback_and_closes_connection(
    monkeypatch,
    tmp_path,
):
    connection = FakeConnection(fail_commit=True)
    repository = make_aggregate(tmp_path / "analysis.sqlite3")[0]
    monkeypatch.setattr(
        "app.datasets.sqlite_repository.sqlite3.connect",
        lambda *args, **kwargs: connection,
    )

    with pytest.raises(sqlite3.OperationalError, match="commit failed"):
        with repository.transaction():
            pass

    assert connection.rollback_called
    assert connection.closed


def test_rollback_failure_chains_body_exception_and_closes_connection(
    monkeypatch,
    tmp_path,
):
    connection = FakeConnection(fail_rollback=True)
    repository = make_aggregate(tmp_path / "analysis.sqlite3")[0]
    monkeypatch.setattr(
        "app.datasets.sqlite_repository.sqlite3.connect",
        lambda *args, **kwargs: connection,
    )
    original_error = RuntimeError("analysis body failed")

    with pytest.raises(sqlite3.OperationalError, match="rollback failed") as raised:
        with repository.transaction():
            raise original_error

    assert raised.value.__cause__ is original_error
    assert connection.rollback_called
    assert connection.closed


def test_begin_immediate_failure_closes_connection(monkeypatch, tmp_path):
    connection = FakeConnection(fail_begin=True)
    repository = make_aggregate(tmp_path / "analysis.sqlite3")[0]
    monkeypatch.setattr(
        "app.datasets.sqlite_repository.sqlite3.connect",
        lambda *args, **kwargs: connection,
    )

    with pytest.raises(sqlite3.OperationalError, match="begin failed"):
        with repository.transaction():
            pytest.fail("The transaction body must not run.")

    assert not connection.rollback_called
    assert connection.closed


def test_rejects_repository_path_and_timeout_mismatches(tmp_path):
    database_path = tmp_path / "analysis.sqlite3"
    other_path = tmp_path / "other.sqlite3"
    schema_repository, incident_repository = make_repositories(database_path)

    with pytest.raises(ValueError, match="database_path"):
        SQLiteDatasetAnalysisRepository(
            other_path,
            schema_repository,
            incident_repository,
        )

    _, different_incident_repository = make_repositories(other_path)
    with pytest.raises(ValueError, match="incident_repository.database_path"):
        SQLiteDatasetAnalysisRepository(
            database_path,
            schema_repository,
            different_incident_repository,
        )

    schema_repository, incident_repository = make_repositories(
        database_path,
        incident_timeout_ms=2500,
    )
    with pytest.raises(ValueError, match="busy_timeout_ms"):
        SQLiteDatasetAnalysisRepository(
            database_path,
            schema_repository,
            incident_repository,
            busy_timeout_ms=2500,
        )

    schema_repository, incident_repository = make_repositories(database_path)
    with pytest.raises(ValueError, match="busy_timeout_ms"):
        SQLiteDatasetAnalysisRepository(
            database_path,
            schema_repository,
            incident_repository,
            busy_timeout_ms=2500,
        )


def test_existing_repositories_still_operate_independently(tmp_path):
    schema_repository, incident_repository = make_repositories(
        tmp_path / "independent.sqlite3"
    )
    baseline, created = schema_repository.get_or_create_initial(
        "customers",
        {"email": "string"},
    )
    assert created
    assert schema_repository.get_current("customers") == baseline

    now = datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc)
    incident = Incident(
        incident_id="independent-incident",
        dataset_id="customers",
        fingerprint="independent-fingerprint",
        code="NULL_VALUES",
        title="Null values found",
        description="A required field contains null values.",
        column="email",
        severity=IncidentSeverity.HIGH,
        created_at=now,
        updated_at=now,
        first_seen_at=now,
        last_seen_at=now,
    )
    with incident_repository.transaction() as unit_of_work:
        unit_of_work.create_incident(incident)

    assert incident_repository.get_incident(incident.incident_id) == incident


class FakeConnection:
    def __init__(
        self,
        *,
        fail_begin=False,
        fail_commit=False,
        fail_rollback=False,
    ):
        self.fail_begin = fail_begin
        self.fail_commit = fail_commit
        self.fail_rollback = fail_rollback
        self.closed = False
        self.in_transaction = False
        self.rollback_called = False
        self.row_factory = None

    def execute(self, statement):
        if statement == "BEGIN IMMEDIATE":
            if self.fail_begin:
                raise sqlite3.OperationalError("begin failed")
            self.in_transaction = True
        return None

    def commit(self):
        if self.fail_commit:
            raise sqlite3.OperationalError("commit failed")
        self.in_transaction = False

    def rollback(self):
        self.rollback_called = True
        if self.fail_rollback:
            raise sqlite3.OperationalError("rollback failed")
        self.in_transaction = False

    def close(self):
        self.closed = True
