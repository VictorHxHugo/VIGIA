"""Framework-independent domain models and rules for incidents."""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class IncidentStatus(str, Enum):
    """Lifecycle states supported by an incident."""

    OPEN = "OPEN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    RESOLVED = "RESOLVED"


class IncidentSeverity(str, Enum):
    """Severity levels accepted from quality issues."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class InvalidIncidentTransitionError(ValueError):
    """Raised when an incident status change is not allowed."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"{field_name} must be a datetime.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone.")
    return value.astimezone(timezone.utc)


def _required_text(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string.")


def _as_status(value: IncidentStatus | str, field_name: str) -> IncidentStatus:
    try:
        return IncidentStatus(value)
    except (TypeError, ValueError) as exc:
        allowed = ", ".join(status.value for status in IncidentStatus)
        raise ValueError(f"{field_name} must be one of: {allowed}.") from exc


def _as_severity(value: IncidentSeverity | str) -> IncidentSeverity:
    try:
        return IncidentSeverity(value)
    except (TypeError, ValueError) as exc:
        allowed = ", ".join(severity.value for severity in IncidentSeverity)
        raise ValueError(f"severity must be one of: {allowed}.") from exc


def generate_incident_fingerprint(
    dataset_id: str,
    code: str,
    column: str | None,
) -> str:
    """Return a stable SHA-256 key for a dataset issue identity."""
    _required_text(dataset_id, "dataset_id")
    _required_text(code, "code")
    if column is not None:
        _required_text(column, "column")

    canonical_identity = json.dumps(
        {
            "code": code,
            "column": column,
            "dataset_id": dataset_id,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical_identity).hexdigest()


@dataclass(frozen=True, slots=True)
class Incident:
    """Current domain state for one deduplicated incident."""

    incident_id: str
    dataset_id: str
    fingerprint: str
    code: str
    title: str
    description: str
    column: str | None
    severity: IncidentSeverity
    status: IncidentStatus = IncidentStatus.OPEN
    created_at: datetime = field(default_factory=_utc_now)
    updated_at: datetime = field(default_factory=_utc_now)
    first_seen_at: datetime = field(default_factory=_utc_now)
    last_seen_at: datetime = field(default_factory=_utc_now)
    resolved_at: datetime | None = None
    occurrence_count: int = 1
    revision: int = 1

    def __post_init__(self) -> None:
        for field_name in (
            "incident_id",
            "dataset_id",
            "fingerprint",
            "code",
            "title",
            "description",
        ):
            _required_text(getattr(self, field_name), field_name)

        if self.column is not None:
            _required_text(self.column, "column")

        object.__setattr__(self, "severity", _as_severity(self.severity))
        object.__setattr__(self, "status", _as_status(self.status, "status"))

        for field_name in (
            "created_at",
            "updated_at",
            "first_seen_at",
            "last_seen_at",
        ):
            object.__setattr__(
                self,
                field_name,
                _as_utc(getattr(self, field_name), field_name),
            )

        if self.resolved_at is not None:
            object.__setattr__(
                self,
                "resolved_at",
                _as_utc(self.resolved_at, "resolved_at"),
            )

        _validate_non_negative_integer(self.occurrence_count, "occurrence_count")
        _validate_non_negative_integer(self.revision, "revision")


@dataclass(frozen=True, slots=True)
class IncidentOccurrence:
    """Immutable snapshot of an issue detected during one analysis."""

    occurrence_id: str
    incident_id: str
    analysis_id: str
    detected_at: datetime
    code: str
    title: str
    description: str
    column: str | None
    severity: IncidentSeverity
    affected_rows: int
    examples: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        for field_name in (
            "occurrence_id",
            "incident_id",
            "analysis_id",
            "code",
            "title",
            "description",
        ):
            _required_text(getattr(self, field_name), field_name)

        if self.column is not None:
            _required_text(self.column, "column")

        object.__setattr__(self, "severity", _as_severity(self.severity))
        object.__setattr__(
            self,
            "detected_at",
            _as_utc(self.detected_at, "detected_at"),
        )
        _validate_non_negative_integer(self.affected_rows, "affected_rows")

        try:
            examples = tuple(self.examples)
        except TypeError as exc:
            raise ValueError("examples must be an iterable of row positions.") from exc

        for example in examples:
            _validate_non_negative_integer(example, "examples entries")
        object.__setattr__(self, "examples", examples)


@dataclass(frozen=True, slots=True)
class IncidentStatusChange:
    """Immutable record of an incident lifecycle transition."""

    change_id: str
    incident_id: str
    from_status: IncidentStatus | None
    to_status: IncidentStatus
    changed_at: datetime
    actor: str
    reason: str

    def __post_init__(self) -> None:
        _required_text(self.change_id, "change_id")
        _required_text(self.incident_id, "incident_id")
        _required_text(self.actor, "actor")
        _required_text(self.reason, "reason")

        if self.from_status is not None:
            object.__setattr__(
                self,
                "from_status",
                _as_status(self.from_status, "from_status"),
            )
        object.__setattr__(
            self,
            "to_status",
            _as_status(self.to_status, "to_status"),
        )
        object.__setattr__(
            self,
            "changed_at",
            _as_utc(self.changed_at, "changed_at"),
        )


def validate_status_transition(
    current_status: IncidentStatus,
    new_status: IncidentStatus,
    *,
    automatic_reopen: bool = False,
) -> bool:
    """Validate a transition; return False when the requested state is unchanged.

    RESOLVED -> OPEN is only permitted when automatic_reopen is true.
    """
    current = _as_status(current_status, "current_status")
    target = _as_status(new_status, "new_status")
    if not isinstance(automatic_reopen, bool):
        raise ValueError("automatic_reopen must be a boolean.")

    if current is target:
        return False

    allowed_transitions = {
        IncidentStatus.OPEN: {
            IncidentStatus.ACKNOWLEDGED,
            IncidentStatus.RESOLVED,
        },
        IncidentStatus.ACKNOWLEDGED: {
            IncidentStatus.OPEN,
            IncidentStatus.RESOLVED,
        },
        IncidentStatus.RESOLVED: (
            {IncidentStatus.OPEN} if automatic_reopen else set()
        ),
    }

    if target not in allowed_transitions[current]:
        raise InvalidIncidentTransitionError(
            f"Transition from {current.value} to {target.value} is not allowed."
        )

    return True


def _validate_non_negative_integer(value: int, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer.")
