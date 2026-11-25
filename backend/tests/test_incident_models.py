from datetime import datetime, timedelta, timezone

import pytest

from app.incidents.models import (
    Incident,
    IncidentOccurrence,
    IncidentSeverity,
    IncidentStatus,
    IncidentStatusChange,
    InvalidIncidentTransitionError,
    generate_incident_fingerprint,
    validate_status_transition,
)


def make_incident(**overrides):
    values = {
        "incident_id": "incident-1",
        "dataset_id": "customers",
        "fingerprint": "fingerprint-1",
        "code": "MISSING_VALUES",
        "title": "Missing values detected",
        "description": "Column 'email' contains missing values.",
        "column": "email",
        "severity": IncidentSeverity.HIGH,
    }
    values.update(overrides)
    return Incident(**values)


def test_fingerprint_is_deterministic():
    first = generate_incident_fingerprint("customers", "MISSING_VALUES", "email")
    second = generate_incident_fingerprint("customers", "MISSING_VALUES", "email")

    assert first == second
    assert len(first) == 64


@pytest.mark.parametrize(
    ("dataset_id", "code", "column"),
    [
        ("orders", "MISSING_VALUES", "email"),
        ("customers", "DUPLICATE_ROWS", "email"),
        ("customers", "MISSING_VALUES", "name"),
    ],
)
def test_fingerprint_changes_when_identity_components_change(
    dataset_id,
    code,
    column,
):
    baseline = generate_incident_fingerprint(
        "customers",
        "MISSING_VALUES",
        "email",
    )

    assert generate_incident_fingerprint(dataset_id, code, column) != baseline


def test_fingerprint_distinguishes_null_column_from_named_column():
    null_column = generate_incident_fingerprint(
        "customers",
        "DUPLICATE_ROWS",
        None,
    )
    named_column = generate_incident_fingerprint(
        "customers",
        "DUPLICATE_ROWS",
        "None",
    )

    assert null_column != named_column


@pytest.mark.parametrize(
    ("current_status", "new_status"),
    [
        (IncidentStatus.OPEN, IncidentStatus.ACKNOWLEDGED),
        (IncidentStatus.OPEN, IncidentStatus.RESOLVED),
        (IncidentStatus.ACKNOWLEDGED, IncidentStatus.OPEN),
        (IncidentStatus.ACKNOWLEDGED, IncidentStatus.RESOLVED),
    ],
)
def test_allows_valid_manual_status_transitions(current_status, new_status):
    assert validate_status_transition(current_status, new_status) is True


@pytest.mark.parametrize(
    ("current_status", "new_status"),
    [
        (IncidentStatus.OPEN, IncidentStatus.OPEN),
        (IncidentStatus.ACKNOWLEDGED, IncidentStatus.ACKNOWLEDGED),
        (IncidentStatus.RESOLVED, IncidentStatus.RESOLVED),
    ],
)
def test_repeating_current_status_does_not_create_a_transition(
    current_status,
    new_status,
):
    assert validate_status_transition(current_status, new_status) is False


@pytest.mark.parametrize(
    ("current_status", "new_status"),
    [
        (IncidentStatus.RESOLVED, IncidentStatus.ACKNOWLEDGED),
    ],
)
def test_rejects_invalid_status_transitions(current_status, new_status):
    with pytest.raises(InvalidIncidentTransitionError):
        validate_status_transition(
            current_status,
            new_status,
            automatic_reopen=True,
        )


def test_resolved_incident_can_only_reopen_automatically():
    with pytest.raises(InvalidIncidentTransitionError):
        validate_status_transition(
            IncidentStatus.RESOLVED,
            IncidentStatus.OPEN,
        )

    assert (
        validate_status_transition(
            IncidentStatus.RESOLVED,
            IncidentStatus.OPEN,
            automatic_reopen=True,
        )
        is True
    )


def test_automatic_reopen_flag_must_be_boolean():
    with pytest.raises(ValueError, match="automatic_reopen"):
        validate_status_transition(
            IncidentStatus.RESOLVED,
            IncidentStatus.OPEN,
            automatic_reopen="yes",
        )


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("severity", "urgent"),
        ("status", "CLOSED"),
    ],
)
def test_incident_rejects_unknown_severity_and_status(field_name, value):
    with pytest.raises(ValueError):
        make_incident(**{field_name: value})


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("occurrence_count", -1),
        ("revision", -1),
    ],
)
def test_incident_rejects_negative_counters(field_name, value):
    with pytest.raises(ValueError):
        make_incident(**{field_name: value})


def test_incident_defaults_to_open_and_uses_utc_timestamps():
    incident = make_incident()

    assert incident.status is IncidentStatus.OPEN
    for timestamp in (
        incident.created_at,
        incident.updated_at,
        incident.first_seen_at,
        incident.last_seen_at,
    ):
        assert timestamp.tzinfo is timezone.utc


def test_incident_normalizes_aware_timestamps_to_utc():
    local_timestamp = datetime(
        2026,
        1,
        1,
        12,
        tzinfo=timezone(timedelta(hours=3)),
    )

    incident = make_incident(created_at=local_timestamp)

    assert incident.created_at == datetime(
        2026,
        1,
        1,
        9,
        tzinfo=timezone.utc,
    )


def test_incident_rejects_naive_timestamps():
    with pytest.raises(ValueError, match="timezone"):
        make_incident(created_at=datetime(2026, 1, 1))


def test_occurrence_snapshots_examples_as_an_immutable_tuple():
    examples = [2, 5]
    occurrence = IncidentOccurrence(
        occurrence_id="occurrence-1",
        incident_id="incident-1",
        analysis_id="analysis-1",
        detected_at=datetime.now(timezone.utc),
        code="MISSING_VALUES",
        title="Missing values detected",
        description="Two missing values were detected.",
        column="email",
        severity="high",
        affected_rows=2,
        examples=examples,
    )
    examples.append(8)

    assert occurrence.examples == (2, 5)
    assert occurrence.severity is IncidentSeverity.HIGH


def test_occurrence_rejects_negative_counts_and_naive_detection_time():
    with pytest.raises(ValueError, match="affected_rows"):
        IncidentOccurrence(
            occurrence_id="occurrence-1",
            incident_id="incident-1",
            analysis_id="analysis-1",
            detected_at=datetime.now(timezone.utc),
            code="MISSING_VALUES",
            title="Missing values detected",
            description="Missing values were detected.",
            column="email",
            severity="high",
            affected_rows=-1,
        )

    with pytest.raises(ValueError, match="timezone"):
        IncidentOccurrence(
            occurrence_id="occurrence-2",
            incident_id="incident-1",
            analysis_id="analysis-2",
            detected_at=datetime(2026, 1, 1),
            code="MISSING_VALUES",
            title="Missing values detected",
            description="Missing values were detected.",
            column="email",
            severity="high",
            affected_rows=1,
        )


def test_status_change_validates_values_and_uses_utc_timestamp():
    change = IncidentStatusChange(
        change_id="change-1",
        incident_id="incident-1",
        from_status="OPEN",
        to_status=IncidentStatus.ACKNOWLEDGED,
        changed_at=datetime.now(timezone.utc),
        actor="analyst-1",
        reason="Investigation started.",
    )

    assert change.from_status is IncidentStatus.OPEN
    assert change.to_status is IncidentStatus.ACKNOWLEDGED
    assert change.changed_at.tzinfo is timezone.utc

    with pytest.raises(ValueError, match="to_status"):
        IncidentStatusChange(
            change_id="change-2",
            incident_id="incident-1",
            from_status=IncidentStatus.OPEN,
            to_status="CLOSED",
            changed_at=datetime.now(timezone.utc),
            actor="analyst-1",
            reason="Invalid target.",
        )
