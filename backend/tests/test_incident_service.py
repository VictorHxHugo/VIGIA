import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from app.incidents import (
    Incident,
    IncidentFilters,
    IncidentProcessingResult,
    IncidentRevisionConflictError,
    IncidentService,
    IncidentSeverity,
    IncidentStatus,
    IncidentStatusChange,
    SQLiteIncidentRepository,
)
from app.quality.schemas import QualityIssue


INITIAL_TIME = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)


def make_issue(
    *,
    code: str = "MISSING_VALUES",
    column: str | None = "email",
    title: str = "Missing values detected",
    description: str = "The column contains missing values.",
    severity: str = "high",
    affected_rows: int = 2,
    examples: list[int] | None = None,
) -> QualityIssue:
    return QualityIssue(
        code=code,
        title=title,
        description=description,
        severity=severity,
        column=column,
        affected_rows=affected_rows,
        examples=examples if examples is not None else [1, 4],
    )


@pytest.fixture
def incident_setup(tmp_path):
    repository = SQLiteIncidentRepository(tmp_path / "incidents.sqlite3")
    return repository, IncidentService(repository)


def find_incident(repository, issue: QualityIssue) -> Incident:
    matches = [
        incident
        for incident in repository.list_incidents(
            IncidentFilters(dataset_id="customers")
        )
        if incident.code == issue.code and incident.column == issue.column
    ]
    assert len(matches) == 1
    return matches[0]


def set_status(
    repository: SQLiteIncidentRepository,
    incident: Incident,
    status: IncidentStatus,
    *,
    resolved_at: datetime | None = None,
) -> Incident:
    changed_at = incident.updated_at + timedelta(minutes=1)
    replacement = replace(
        incident,
        status=status,
        resolved_at=resolved_at,
        updated_at=changed_at,
        revision=incident.revision,
    )
    with repository.transaction() as unit_of_work:
        updated = unit_of_work.update_incident(
            replacement,
            expected_revision=incident.revision,
        )
        unit_of_work.record_status_change(
            IncidentStatusChange(
                change_id=f"manual-{status.value.lower()}",
                incident_id=incident.incident_id,
                from_status=incident.status,
                to_status=status,
                changed_at=changed_at,
                actor="operator",
                reason="Test status update.",
            )
        )
    return updated


def test_creates_incident_occurrence_and_initial_status_event(incident_setup):
    repository, service = incident_setup
    issue = make_issue()

    result = service.process_issues(
        "customers",
        "analysis-1",
        [issue],
        detected_at=INITIAL_TIME,
    )

    incident = find_incident(repository, issue)
    occurrences = repository.list_occurrences(incident.incident_id)
    history = repository.list_status_history(incident.incident_id)

    assert result == IncidentProcessingResult(created_count=1)
    assert incident.status is IncidentStatus.OPEN
    assert incident.occurrence_count == 1
    assert incident.revision == 1
    assert (
        incident.created_at
        == incident.updated_at
        == incident.first_seen_at
        == incident.last_seen_at
        == INITIAL_TIME
    )
    assert len(occurrences) == 1
    assert len(history) == 1
    assert history[0].from_status is None
    assert history[0].to_status is IncidentStatus.OPEN
    assert history[0].actor == "system"


def test_existing_open_incident_is_updated_without_status_change(
    incident_setup,
    monkeypatch,
):
    repository, service = incident_setup
    issue = make_issue()
    service.process_issues(
        "customers",
        "analysis-1",
        [issue],
        detected_at=INITIAL_TIME,
    )
    monkeypatch.setattr(
        "app.incidents.service.IncidentService._utc_now",
        staticmethod(lambda: INITIAL_TIME + timedelta(hours=1)),
    )

    result = service.process_issues(
        "customers",
        "analysis-2",
        [
            make_issue(
                title="Updated title",
                description="Updated description.",
                severity="critical",
            )
        ],
        detected_at=INITIAL_TIME + timedelta(minutes=30),
    )

    incident = find_incident(repository, issue)
    assert result == IncidentProcessingResult(updated_count=1)
    assert incident.status is IncidentStatus.OPEN
    assert incident.occurrence_count == 2
    assert incident.title == "Updated title"
    assert incident.description == "Updated description."
    assert incident.severity is IncidentSeverity.CRITICAL
    assert incident.updated_at == INITIAL_TIME + timedelta(hours=1)
    assert len(repository.list_status_history(incident.incident_id)) == 1


def test_acknowledged_incident_remains_acknowledged(incident_setup):
    repository, service = incident_setup
    issue = make_issue()
    service.process_issues("customers", "analysis-1", [issue], detected_at=INITIAL_TIME)
    incident = set_status(
        repository,
        find_incident(repository, issue),
        IncidentStatus.ACKNOWLEDGED,
    )

    result = service.process_issues(
        "customers",
        "analysis-2",
        [issue],
        detected_at=INITIAL_TIME + timedelta(hours=1),
    )

    current = repository.get_incident(incident.incident_id)
    assert current is not None
    assert current.status is IncidentStatus.ACKNOWLEDGED
    assert current.occurrence_count == 2
    assert result.updated_count == 1
    assert result.reopened_count == 0
    assert len(repository.list_status_history(incident.incident_id)) == 2


def test_resolved_incident_reopens_and_records_transition(incident_setup):
    repository, service = incident_setup
    issue = make_issue()
    service.process_issues("customers", "analysis-1", [issue], detected_at=INITIAL_TIME)
    resolved_at = INITIAL_TIME + timedelta(hours=1)
    incident = set_status(
        repository,
        find_incident(repository, issue),
        IncidentStatus.RESOLVED,
        resolved_at=resolved_at,
    )

    result = service.process_issues(
        "customers",
        "analysis-2",
        [issue],
        detected_at=INITIAL_TIME + timedelta(hours=2),
    )

    reopened = repository.get_incident(incident.incident_id)
    assert reopened is not None
    assert reopened.status is IncidentStatus.OPEN
    assert reopened.resolved_at is None
    assert reopened.occurrence_count == 2
    assert result.updated_count == 1
    assert result.reopened_count == 1

    history = repository.list_status_history(incident.incident_id)
    assert [(change.from_status, change.to_status) for change in history][-1] == (
        IncidentStatus.RESOLVED,
        IncidentStatus.OPEN,
    )
    assert history[-1].actor == "system"
    assert "detected again" in history[-1].reason


def test_first_seen_is_preserved_and_last_seen_advances(incident_setup):
    repository, service = incident_setup
    issue = make_issue()
    service.process_issues(
        "customers",
        "analysis-1",
        [issue],
        detected_at=INITIAL_TIME,
    )

    later_detection = INITIAL_TIME + timedelta(days=1)
    service.process_issues(
        "customers",
        "analysis-2",
        [issue],
        detected_at=later_detection,
    )

    incident = find_incident(repository, issue)
    assert incident.first_seen_at == INITIAL_TIME
    assert incident.last_seen_at == later_detection


def test_last_seen_does_not_move_back_for_late_processed_analysis(
    incident_setup,
    monkeypatch,
):
    repository, service = incident_setup
    issue = make_issue()
    execution_times = iter(
        [
            INITIAL_TIME + timedelta(hours=1),
            INITIAL_TIME + timedelta(days=3, hours=1),
            INITIAL_TIME + timedelta(days=5),
        ]
    )
    monkeypatch.setattr(
        "app.incidents.service.IncidentService._utc_now",
        staticmethod(lambda: next(execution_times)),
    )
    service.process_issues(
        "customers",
        "analysis-1",
        [issue],
        detected_at=INITIAL_TIME,
    )
    newest_detection = INITIAL_TIME + timedelta(days=3)
    service.process_issues(
        "customers",
        "analysis-2",
        [issue],
        detected_at=newest_detection,
    )
    processing_time = INITIAL_TIME + timedelta(days=5)

    service.process_issues(
        "customers",
        "analysis-3",
        [issue],
        detected_at=INITIAL_TIME + timedelta(days=1),
    )

    incident = find_incident(repository, issue)
    assert incident.first_seen_at == INITIAL_TIME
    assert incident.last_seen_at == newest_detection
    assert incident.updated_at == processing_time


def test_updated_at_does_not_move_back_when_execution_time_is_older(
    incident_setup,
    monkeypatch,
):
    repository, service = incident_setup
    issue = make_issue()
    service.process_issues(
        "customers",
        "analysis-1",
        [issue],
        detected_at=INITIAL_TIME,
    )
    incident = find_incident(repository, issue)
    persisted_updated_at = INITIAL_TIME + timedelta(hours=4)
    persisted_last_seen_at = INITIAL_TIME + timedelta(hours=5)

    with repository.transaction() as unit_of_work:
        incident = unit_of_work.update_incident(
            replace(
                incident,
                updated_at=persisted_updated_at,
                last_seen_at=persisted_last_seen_at,
            ),
            expected_revision=incident.revision,
        )

    execution_time = INITIAL_TIME + timedelta(hours=2)
    monkeypatch.setattr(
        "app.incidents.service.IncidentService._utc_now",
        staticmethod(lambda: execution_time),
    )
    detection_time = INITIAL_TIME + timedelta(hours=3)

    result = service.process_issues(
        "customers",
        "analysis-2",
        [issue],
        detected_at=detection_time,
    )

    updated = repository.get_incident(incident.incident_id)
    assert updated is not None
    assert result.updated_count == 1
    assert updated.updated_at == persisted_updated_at
    assert updated.occurrence_count == 2
    assert updated.last_seen_at == persisted_last_seen_at
    assert len(repository.list_occurrences(incident.incident_id)) == 2


def test_same_analysis_is_idempotent_for_occurrence_and_aggregate(incident_setup):
    repository, service = incident_setup
    issue = make_issue()
    first = service.process_issues(
        "customers",
        "analysis-1",
        [issue],
        detected_at=INITIAL_TIME,
    )
    duplicate = service.process_issues(
        "customers",
        "analysis-1",
        [make_issue(title="Retry title", severity="low")],
        detected_at=INITIAL_TIME + timedelta(days=1),
    )

    incident = find_incident(repository, issue)
    assert first.created_count == 1
    assert duplicate == IncidentProcessingResult(ignored_occurrence_count=1)
    assert incident.occurrence_count == 1
    assert incident.title == issue.title
    assert incident.severity is IncidentSeverity.HIGH
    assert incident.last_seen_at == INITIAL_TIME
    assert len(repository.list_occurrences(incident.incident_id)) == 1
    assert len(repository.list_status_history(incident.incident_id)) == 1


def test_duplicate_fingerprints_inside_input_are_deduplicated(incident_setup):
    repository, service = incident_setup

    result = service.process_issues(
        "customers",
        "analysis-1",
        [
            make_issue(),
            make_issue(title="Duplicate issue", severity="low"),
        ],
        detected_at=INITIAL_TIME,
    )

    incident = find_incident(repository, make_issue())
    assert result == IncidentProcessingResult(
        created_count=1,
        ignored_occurrence_count=1,
    )
    assert incident.occurrence_count == 1
    assert incident.title == "Missing values detected"
    assert len(repository.list_occurrences(incident.incident_id)) == 1


def test_distinct_fingerprints_create_distinct_incidents(incident_setup):
    repository, service = incident_setup

    result = service.process_issues(
        "customers",
        "analysis-1",
        [
            make_issue(code="MISSING_VALUES", column="email"),
            make_issue(code="DUPLICATE_VALUES", column="email"),
            make_issue(code="MISSING_VALUES", column="name"),
        ],
        detected_at=INITIAL_TIME,
    )

    assert result.created_count == 3
    assert len(repository.list_incidents(IncidentFilters(dataset_id="customers"))) == 3


def test_quality_issue_severity_and_examples_are_converted(incident_setup):
    repository, service = incident_setup
    issue = make_issue(severity="medium", examples=[0, 3, 5])
    service.process_issues(
        "customers",
        "analysis-1",
        [issue],
        detected_at=INITIAL_TIME,
    )

    incident = find_incident(repository, issue)
    occurrence = repository.list_occurrences(incident.incident_id)[0]
    assert incident.severity is IncidentSeverity.MEDIUM
    assert occurrence.severity is IncidentSeverity.MEDIUM
    assert occurrence.examples == (0, 3, 5)
    assert occurrence.affected_rows == 2


def test_failure_after_writes_rolls_back_incident_occurrence_and_history(
    incident_setup,
):
    repository, service = incident_setup
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_initial_status_change
            BEFORE INSERT ON incident_status_history
            BEGIN
                SELECT RAISE(ABORT, 'forced status history failure');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="forced status history failure"):
        service.process_issues(
            "customers",
            "analysis-1",
            [make_issue()],
            detected_at=INITIAL_TIME,
        )

    with sqlite3.connect(repository.database_path) as connection:
        incident_count = connection.execute(
            "SELECT COUNT(*) FROM incidents",
        ).fetchone()[0]
        occurrence_count = connection.execute(
            "SELECT COUNT(*) FROM incident_occurrences",
        ).fetchone()[0]
        status_change_count = connection.execute(
            "SELECT COUNT(*) FROM incident_status_history",
        ).fetchone()[0]
    assert (incident_count, occurrence_count, status_change_count) == (0, 0, 0)


def test_revision_conflict_is_propagated_and_transaction_rolls_back(
    incident_setup,
):
    repository, _ = incident_setup
    service = IncidentService(_ConflictOnUpdateRepository(repository))
    issue = make_issue()
    original_service = IncidentService(repository)
    original_service.process_issues(
        "customers",
        "analysis-1",
        [issue],
        detected_at=INITIAL_TIME,
    )
    incident = find_incident(repository, issue)

    with pytest.raises(IncidentRevisionConflictError):
        service.process_issues(
            "customers",
            "analysis-2",
            [issue],
            detected_at=INITIAL_TIME + timedelta(hours=1),
        )

    current = repository.get_incident(incident.incident_id)
    assert current is not None
    assert current.occurrence_count == 1
    assert len(repository.list_occurrences(incident.incident_id)) == 1


class _ConflictOnUpdateRepository:
    def __init__(self, repository: SQLiteIncidentRepository) -> None:
        self._repository = repository

    @contextmanager
    def transaction(self):
        with self._repository.transaction() as unit_of_work:
            yield _ConflictOnUpdateUnitOfWork(unit_of_work)


class _ConflictOnUpdateUnitOfWork:
    def __init__(self, unit_of_work) -> None:
        self._unit_of_work = unit_of_work

    def __getattr__(self, name):
        return getattr(self._unit_of_work, name)

    def update_incident(self, incident, *, expected_revision):
        raise IncidentRevisionConflictError(
            incident.incident_id,
            expected_revision,
            expected_revision + 1,
        )


def test_empty_issues_returns_zero_counts_without_persisting(incident_setup):
    repository, service = incident_setup

    result = service.process_issues(
        "customers",
        "analysis-empty",
        [],
        detected_at=INITIAL_TIME,
    )

    assert result == IncidentProcessingResult()
    assert repository.list_incidents(IncidentFilters(dataset_id="customers")) == []


@pytest.mark.parametrize(
    ("dataset_id", "analysis_id", "issues", "detected_at", "message"),
    [
        ("", "analysis-1", [], INITIAL_TIME, "dataset_id"),
        (" customers", "analysis-1", [], INITIAL_TIME, "dataset_id"),
        ("customers", " ", [], INITIAL_TIME, "analysis_id"),
        ("customers", "analysis-1", "not a sequence of issues", None, "issues"),
        ("customers", "analysis-1", [object()], None, "QualityIssue"),
        ("customers", "analysis-1", [], datetime(2026, 1, 1), "timezone"),
        ("customers", "analysis-1", [], "not a datetime", "datetime"),
    ],
)
def test_validates_process_inputs(
    incident_setup,
    dataset_id,
    analysis_id,
    issues,
    detected_at,
    message,
):
    _, service = incident_setup

    with pytest.raises(ValueError, match=message):
        service.process_issues(
            dataset_id,
            analysis_id,
            issues,
            detected_at=detected_at,
        )


def test_detected_at_is_normalized_to_utc(incident_setup):
    repository, service = incident_setup
    offset_time = datetime(
        2026,
        1,
        1,
        15,
        tzinfo=timezone(timedelta(hours=3)),
    )
    service.process_issues(
        "customers",
        "analysis-1",
        [make_issue()],
        detected_at=offset_time,
    )

    incident = find_incident(repository, make_issue())
    assert incident.created_at == INITIAL_TIME
    assert incident.created_at.tzinfo is timezone.utc


def test_service_uses_one_execution_timestamp_for_multiple_new_incidents(
    incident_setup,
    monkeypatch,
):
    repository, service = incident_setup
    execution_time = INITIAL_TIME + timedelta(hours=2)
    monkeypatch.setattr(
        "app.incidents.service.IncidentService._utc_now",
        staticmethod(lambda: execution_time),
    )

    service.process_issues(
        "customers",
        "analysis-1",
        [
            make_issue(code="MISSING_VALUES", column="email"),
            make_issue(code="DUPLICATE_VALUES", column="email"),
        ],
    )

    incidents = repository.list_incidents(IncidentFilters(dataset_id="customers"))
    assert len(incidents) == 2
    assert all(incident.created_at == execution_time for incident in incidents)
