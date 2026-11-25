import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from app.incidents import (
    Incident,
    IncidentFilters,
    IncidentNotFoundError,
    IncidentOccurrence,
    IncidentRepository,
    IncidentRevisionConflictError,
    IncidentSeverity,
    IncidentStatus,
    IncidentStatusChange,
    InvalidIncidentTransitionError,
    SQLiteIncidentRepository,
)
from app.incidents.management_service import IncidentManagementService


BASE_TIME = datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc)


def make_incident(
    *,
    incident_id: str = "incident-1",
    status: IncidentStatus = IncidentStatus.OPEN,
    revision: int = 1,
    updated_at: datetime = BASE_TIME,
    resolved_at: datetime | None = None,
) -> Incident:
    return Incident(
        incident_id=incident_id,
        dataset_id="customers",
        fingerprint=f"fingerprint-{incident_id}",
        code="NULL_VALUES",
        title="Null values found",
        description="A required field contains null values.",
        column="email",
        severity=IncidentSeverity.HIGH,
        status=status,
        created_at=BASE_TIME,
        updated_at=updated_at,
        first_seen_at=BASE_TIME - timedelta(days=1),
        last_seen_at=updated_at,
        resolved_at=resolved_at,
        occurrence_count=1,
        revision=revision,
    )


def make_occurrence(
    occurrence_id: str,
    *,
    incident_id: str = "incident-1",
    detected_at: datetime = BASE_TIME,
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
        affected_rows=3,
        examples=(2, 4, 9),
    )


def make_status_change(
    change_id: str,
    from_status: IncidentStatus | None,
    to_status: IncidentStatus,
    *,
    incident_id: str = "incident-1",
) -> IncidentStatusChange:
    return IncidentStatusChange(
        change_id=change_id,
        incident_id=incident_id,
        from_status=from_status,
        to_status=to_status,
        changed_at=BASE_TIME,
        actor="operator@example.test",
        reason="Investigating the alert.",
    )


@pytest.fixture
def incident_setup(tmp_path):
    repository = SQLiteIncidentRepository(tmp_path / "incidents.sqlite3")
    return repository, IncidentManagementService(repository)


def persist_incident(repository: SQLiteIncidentRepository, incident: Incident) -> None:
    with repository.transaction() as unit_of_work:
        unit_of_work.create_incident(incident)


def test_list_incidents_delegates_filters_to_repository():
    repository = Mock(spec=IncidentRepository)
    service = IncidentManagementService(repository)
    filters = IncidentFilters(
        dataset_id="customers",
        status=IncidentStatus.OPEN,
        limit=10,
        offset=5,
    )
    expected = [make_incident()]
    repository.list_incidents.return_value = expected

    result = service.list_incidents(filters)

    assert result is expected
    repository.list_incidents.assert_called_once_with(filters)


def test_get_incident_returns_existing_incident(incident_setup):
    repository, service = incident_setup
    incident = make_incident()
    persist_incident(repository, incident)

    result = service.get_incident(incident.incident_id)

    assert result == incident


def test_get_incident_raises_when_incident_does_not_exist(incident_setup):
    _, service = incident_setup

    with pytest.raises(IncidentNotFoundError) as error:
        service.get_incident("missing-incident")

    assert error.value.incident_id == "missing-incident"


def test_list_occurrences_returns_requested_page(incident_setup):
    repository, service = incident_setup
    incident = make_incident()
    first = make_occurrence("occurrence-1", detected_at=BASE_TIME)
    second = make_occurrence(
        "occurrence-2",
        detected_at=BASE_TIME + timedelta(minutes=1),
    )
    persist_incident(repository, incident)
    with repository.transaction() as unit_of_work:
        unit_of_work.record_occurrence(first)
        unit_of_work.record_occurrence(second)

    result = service.list_occurrences(
        incident.incident_id,
        limit=1,
        offset=1,
    )

    assert result == [first]


def test_list_status_history_returns_requested_page(incident_setup):
    repository, service = incident_setup
    incident = make_incident()
    first = make_status_change(
        "change-1",
        None,
        IncidentStatus.OPEN,
    )
    second = make_status_change(
        "change-2",
        IncidentStatus.OPEN,
        IncidentStatus.ACKNOWLEDGED,
    )
    persist_incident(repository, incident)
    with repository.transaction() as unit_of_work:
        unit_of_work.record_status_change(first)
        unit_of_work.record_status_change(second)

    result = service.list_status_history(
        incident.incident_id,
        limit=1,
        offset=1,
    )

    assert result == [second]


def test_occurrences_and_history_return_empty_lists_for_existing_incident(
    incident_setup,
):
    repository, service = incident_setup
    incident = make_incident()
    persist_incident(repository, incident)

    assert service.list_occurrences(incident.incident_id) == []
    assert service.list_status_history(incident.incident_id) == []


@pytest.mark.parametrize(
    "method_name",
    ["list_occurrences", "list_status_history"],
)
def test_occurrences_and_history_raise_for_missing_incident(
    incident_setup,
    method_name,
):
    _, service = incident_setup

    with pytest.raises(IncidentNotFoundError) as error:
        getattr(service, method_name)(
            "missing-incident",
            limit=10,
            offset=2,
        )

    assert error.value.incident_id == "missing-incident"


@pytest.mark.parametrize(
    ("current_status", "target_status"),
    [
        (IncidentStatus.OPEN, IncidentStatus.ACKNOWLEDGED),
        (IncidentStatus.OPEN, IncidentStatus.RESOLVED),
        (IncidentStatus.ACKNOWLEDGED, IncidentStatus.RESOLVED),
        (IncidentStatus.ACKNOWLEDGED, IncidentStatus.OPEN),
    ],
)
def test_valid_status_changes_persist_revision_and_history(
    incident_setup,
    current_status,
    target_status,
):
    repository, service = incident_setup
    incident = make_incident(status=current_status)
    persist_incident(repository, incident)

    result = service.change_status(
        incident.incident_id,
        target_status,
        expected_revision=incident.revision,
        actor="operator@example.test",
        reason="Change approved.",
    )

    persisted = repository.get_incident(incident.incident_id)
    history = repository.list_status_history(incident.incident_id)
    assert persisted is not None
    assert result == persisted
    assert persisted.status is target_status
    assert persisted.revision == incident.revision + 1
    assert len(history) == 1
    assert history[0].from_status is current_status
    assert history[0].to_status is target_status
    assert history[0].actor == "operator@example.test"
    assert history[0].reason == "Change approved."


def test_invalid_status_transition_is_rejected_without_persisting_changes(
    incident_setup,
):
    repository, service = incident_setup
    incident = make_incident(
        status=IncidentStatus.RESOLVED,
        resolved_at=BASE_TIME,
    )
    persist_incident(repository, incident)

    with pytest.raises(InvalidIncidentTransitionError):
        service.change_status(
            incident.incident_id,
            IncidentStatus.ACKNOWLEDGED,
            expected_revision=incident.revision,
            actor="operator@example.test",
            reason="Invalid transition.",
        )

    assert repository.get_incident(incident.incident_id) == incident
    assert repository.list_status_history(incident.incident_id) == []


def test_manual_reopening_of_resolved_incident_is_rejected(incident_setup):
    repository, service = incident_setup
    incident = make_incident(
        status=IncidentStatus.RESOLVED,
        resolved_at=BASE_TIME,
    )
    persist_incident(repository, incident)

    with pytest.raises(InvalidIncidentTransitionError):
        service.change_status(
            incident.incident_id,
            IncidentStatus.OPEN,
            expected_revision=incident.revision,
            actor="operator@example.test",
            reason="Manual reopen attempt.",
        )

    assert repository.get_incident(incident.incident_id) == incident
    assert repository.list_status_history(incident.incident_id) == []


def test_stale_expected_revision_raises_conflict_without_changes(incident_setup):
    repository, service = incident_setup
    incident = make_incident()
    persist_incident(repository, incident)

    with pytest.raises(IncidentRevisionConflictError) as error:
        service.change_status(
            incident.incident_id,
            IncidentStatus.ACKNOWLEDGED,
            expected_revision=incident.revision + 1,
            actor="operator@example.test",
            reason="Stale update.",
        )

    assert error.value.incident_id == incident.incident_id
    assert error.value.expected_revision == incident.revision + 1
    assert error.value.current_revision == incident.revision
    assert repository.get_incident(incident.incident_id) == incident
    assert repository.list_status_history(incident.incident_id) == []


@pytest.mark.parametrize(
    "expected_revision",
    [True, False, 0, -1, "1", 1.0, None],
)
def test_invalid_expected_revision_is_rejected_without_changes(
    incident_setup,
    expected_revision,
):
    repository, service = incident_setup
    incident = make_incident()
    persist_incident(repository, incident)

    with pytest.raises(ValueError, match="expected_revision"):
        service.change_status(
            incident.incident_id,
            IncidentStatus.ACKNOWLEDGED,
            expected_revision=expected_revision,
            actor="operator@example.test",
            reason="Invalid revision.",
        )

    assert repository.get_incident(incident.incident_id) == incident
    assert repository.list_status_history(incident.incident_id) == []


def test_repeating_current_status_does_not_increment_revision_or_add_history(
    incident_setup,
):
    repository, service = incident_setup
    incident = make_incident()
    persist_incident(repository, incident)

    result = service.change_status(
        incident.incident_id,
        incident.status,
        expected_revision=incident.revision,
        actor="operator@example.test",
        reason="No status change.",
    )

    assert result == incident
    assert repository.get_incident(incident.incident_id) == incident
    assert repository.list_status_history(incident.incident_id) == []


def test_resolving_sets_resolved_at(incident_setup):
    repository, service = incident_setup
    incident = make_incident(status=IncidentStatus.OPEN)
    persist_incident(repository, incident)

    result = service.change_status(
        incident.incident_id,
        IncidentStatus.RESOLVED,
        expected_revision=incident.revision,
        actor="operator@example.test",
        reason="Issue resolved.",
    )

    assert result.resolved_at is not None
    assert result.resolved_at.utcoffset() == timedelta(0)
    assert result.status is IncidentStatus.RESOLVED
    assert repository.get_incident(incident.incident_id) == result


def test_non_resolved_target_clears_stale_resolved_at(incident_setup):
    repository, service = incident_setup
    incident = make_incident(
        status=IncidentStatus.ACKNOWLEDGED,
        resolved_at=BASE_TIME,
    )
    persist_incident(repository, incident)

    result = service.change_status(
        incident.incident_id,
        IncidentStatus.OPEN,
        expected_revision=incident.revision,
        actor="operator@example.test",
        reason="Resume investigation.",
    )

    assert result.status is IncidentStatus.OPEN
    assert result.resolved_at is None
    assert repository.get_incident(incident.incident_id) == result


def test_updated_at_does_not_move_back(incident_setup):
    repository, service = incident_setup
    persisted_updated_at = datetime.now(timezone.utc) + timedelta(days=1)
    incident = make_incident(
        status=IncidentStatus.ACKNOWLEDGED,
        updated_at=persisted_updated_at,
    )
    persist_incident(repository, incident)

    result = service.change_status(
        incident.incident_id,
        IncidentStatus.OPEN,
        expected_revision=incident.revision,
        actor="operator@example.test",
        reason="Resume investigation.",
    )

    persisted = repository.get_incident(incident.incident_id)
    assert persisted is not None
    assert result.updated_at == persisted_updated_at
    assert persisted.updated_at == persisted_updated_at


def test_history_write_failure_rolls_back_incident_update(incident_setup):
    repository, service = incident_setup
    incident = make_incident()
    persist_incident(repository, incident)
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_status_change
            BEFORE INSERT ON incident_status_history
            BEGIN
                SELECT RAISE(ABORT, 'forced status history failure');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="forced status history failure"):
        service.change_status(
            incident.incident_id,
            IncidentStatus.ACKNOWLEDGED,
            expected_revision=incident.revision,
            actor="operator@example.test",
            reason="This update must roll back.",
        )

    assert repository.get_incident(incident.incident_id) == incident
    assert repository.list_status_history(incident.incident_id) == []
