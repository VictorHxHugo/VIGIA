"""Application service for persisting incidents from detected quality issues."""

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from uuid import uuid4

from app.incidents.models import (
    Incident,
    IncidentOccurrence,
    IncidentSeverity,
    IncidentStatus,
    IncidentStatusChange,
    generate_incident_fingerprint,
    validate_status_transition,
)
from app.incidents.repository import IncidentRepository, IncidentUnitOfWork
from app.quality.schemas import QualityIssue


_SYSTEM_ACTOR = "system"
_INITIAL_STATUS_REASON = "Incident created from a detected quality issue."
_REOPEN_REASON = "The quality issue was detected again after resolution."


@dataclass(frozen=True, slots=True)
class IncidentProcessingResult:
    """Counts of incident changes made while processing one analysis.

    Reopened incidents are included in ``updated_count``.
    """

    created_count: int = 0
    updated_count: int = 0
    reopened_count: int = 0
    ignored_occurrence_count: int = 0


class IncidentService:
    """Convert quality issues into persistent, idempotent incident events."""

    def __init__(self, repository: IncidentRepository) -> None:
        self._repository = repository

    def process_issues(
        self,
        dataset_id: str,
        analysis_id: str,
        issues: Sequence[QualityIssue],
        *,
        detected_at: datetime | None = None,
    ) -> IncidentProcessingResult:
        """Create or update incidents and record their occurrences atomically."""
        self._validate_identifier(dataset_id, "dataset_id")
        self._validate_identifier(analysis_id, "analysis_id")
        issue_snapshot = self._validate_issues(issues)

        execution_time = self._utc_now()
        detection_time = (
            self._normalize_datetime(detected_at)
            if detected_at is not None
            else execution_time
        )

        prepared_issues: list[tuple[QualityIssue, str, IncidentSeverity]] = []
        seen_fingerprints: set[str] = set()
        ignored_count = 0
        for issue in issue_snapshot:
            fingerprint = generate_incident_fingerprint(
                dataset_id,
                issue.code,
                issue.column,
            )
            if fingerprint in seen_fingerprints:
                ignored_count += 1
                continue
            seen_fingerprints.add(fingerprint)
            prepared_issues.append(
                (issue, fingerprint, self._convert_severity(issue.severity))
            )

        if not prepared_issues:
            return IncidentProcessingResult(
                ignored_occurrence_count=ignored_count,
            )

        created_count = 0
        updated_count = 0
        reopened_count = 0

        with self._repository.transaction() as unit_of_work:
            for issue, fingerprint, severity in prepared_issues:
                incident = unit_of_work.get_by_fingerprint(
                    dataset_id,
                    fingerprint,
                )
                if incident is None:
                    self._create_incident(
                        unit_of_work,
                        issue,
                        dataset_id,
                        analysis_id,
                        fingerprint,
                        severity,
                        detection_time,
                    )
                    created_count += 1
                    continue

                occurrence = self._make_occurrence(
                    issue,
                    incident.incident_id,
                    analysis_id,
                    detection_time,
                    severity,
                )
                if not unit_of_work.record_occurrence(occurrence):
                    ignored_count += 1
                    continue

                reopened = incident.status is IncidentStatus.RESOLVED
                new_status = incident.status
                resolved_at = incident.resolved_at
                if reopened:
                    validate_status_transition(
                        incident.status,
                        IncidentStatus.OPEN,
                        automatic_reopen=True,
                    )
                    new_status = IncidentStatus.OPEN
                    resolved_at = None

                updated_incident = replace(
                    incident,
                    title=issue.title,
                    description=issue.description,
                    severity=severity,
                    status=new_status,
                    updated_at=max(incident.updated_at, execution_time),
                    last_seen_at=max(incident.last_seen_at, detection_time),
                    resolved_at=resolved_at,
                    occurrence_count=incident.occurrence_count + 1,
                )
                unit_of_work.update_incident(
                    updated_incident,
                    expected_revision=incident.revision,
                )

                if reopened:
                    unit_of_work.record_status_change(
                        IncidentStatusChange(
                            change_id=self._new_id(),
                            incident_id=incident.incident_id,
                            from_status=IncidentStatus.RESOLVED,
                            to_status=IncidentStatus.OPEN,
                            changed_at=execution_time,
                            actor=_SYSTEM_ACTOR,
                            reason=_REOPEN_REASON,
                        )
                    )
                    reopened_count += 1

                updated_count += 1

        return IncidentProcessingResult(
            created_count=created_count,
            updated_count=updated_count,
            reopened_count=reopened_count,
            ignored_occurrence_count=ignored_count,
        )

    def _create_incident(
        self,
        unit_of_work: IncidentUnitOfWork,
        issue: QualityIssue,
        dataset_id: str,
        analysis_id: str,
        fingerprint: str,
        severity: IncidentSeverity,
        detection_time: datetime,
    ) -> None:
        incident_id = self._new_id()
        incident = Incident(
            incident_id=incident_id,
            dataset_id=dataset_id,
            fingerprint=fingerprint,
            code=issue.code,
            title=issue.title,
            description=issue.description,
            column=issue.column,
            severity=severity,
            status=IncidentStatus.OPEN,
            created_at=detection_time,
            updated_at=detection_time,
            first_seen_at=detection_time,
            last_seen_at=detection_time,
            occurrence_count=1,
            revision=1,
        )
        unit_of_work.create_incident(incident)

        occurrence = self._make_occurrence(
            issue,
            incident_id,
            analysis_id,
            detection_time,
            severity,
        )
        if not unit_of_work.record_occurrence(occurrence):
            raise RuntimeError(
                "A new incident unexpectedly had a duplicate analysis occurrence."
            )

        unit_of_work.record_status_change(
            IncidentStatusChange(
                change_id=self._new_id(),
                incident_id=incident_id,
                from_status=None,
                to_status=IncidentStatus.OPEN,
                changed_at=detection_time,
                actor=_SYSTEM_ACTOR,
                reason=_INITIAL_STATUS_REASON,
            )
        )

    @staticmethod
    def _make_occurrence(
        issue: QualityIssue,
        incident_id: str,
        analysis_id: str,
        detected_at: datetime,
        severity: IncidentSeverity,
    ) -> IncidentOccurrence:
        return IncidentOccurrence(
            occurrence_id=IncidentService._new_id(),
            incident_id=incident_id,
            analysis_id=analysis_id,
            detected_at=detected_at,
            code=issue.code,
            title=issue.title,
            description=issue.description,
            column=issue.column,
            severity=severity,
            affected_rows=issue.affected_rows,
            examples=tuple(issue.examples),
        )

    @staticmethod
    def _validate_identifier(value: str, field_name: str) -> None:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field_name} must be a non-empty string.")
        if value != value.strip():
            raise ValueError(f"{field_name} must not have surrounding whitespace.")

    @staticmethod
    def _validate_issues(
        issues: Sequence[QualityIssue],
    ) -> tuple[QualityIssue, ...]:
        if (
            isinstance(issues, (str, bytes, bytearray))
            or not isinstance(issues, Sequence)
        ):
            raise ValueError("issues must be a sequence of QualityIssue instances.")

        issue_snapshot = tuple(issues)
        if any(not isinstance(issue, QualityIssue) for issue in issue_snapshot):
            raise ValueError("Every item in issues must be a QualityIssue.")
        return issue_snapshot

    @staticmethod
    def _convert_severity(value: str) -> IncidentSeverity:
        try:
            return IncidentSeverity(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Unsupported incident severity: {value!r}.") from exc

    @staticmethod
    def _normalize_datetime(value: datetime) -> datetime:
        if not isinstance(value, datetime):
            raise ValueError("detected_at must be a datetime.")
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("detected_at must include a timezone.")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _utc_now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _new_id() -> str:
        return str(uuid4())


__all__ = ["IncidentProcessingResult", "IncidentService"]
