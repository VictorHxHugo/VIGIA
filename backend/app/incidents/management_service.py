"""Application service for querying and manually managing incidents."""

from dataclasses import replace
from datetime import datetime, timezone
from uuid import uuid4

from app.incidents.models import (
    Incident,
    IncidentOccurrence,
    IncidentStatus,
    IncidentStatusChange,
    validate_status_transition,
)
from app.incidents.repository import (
    IncidentFilters,
    IncidentNotFoundError,
    IncidentRepository,
    IncidentRevisionConflictError,
)


class IncidentManagementService:
    """Provide incident queries and transactional manual status changes."""

    def __init__(self, repository: IncidentRepository) -> None:
        self._repository = repository

    def list_incidents(self, filters: IncidentFilters) -> list[Incident]:
        """Return incidents matching the supplied filters."""
        return self._repository.list_incidents(filters)

    def get_incident(self, incident_id: str) -> Incident:
        """Return an incident or raise IncidentNotFoundError."""
        incident = self._repository.get_incident(incident_id)
        if incident is None:
            raise IncidentNotFoundError(incident_id)
        return incident

    def list_occurrences(
        self,
        incident_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> list[IncidentOccurrence]:
        """Return paginated occurrences for an existing incident."""
        self.get_incident(incident_id)
        return self._repository.list_occurrences(
            incident_id,
            limit=limit,
            offset=offset,
        )

    def list_status_history(
        self,
        incident_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> list[IncidentStatusChange]:
        """Return paginated status history for an existing incident."""
        self.get_incident(incident_id)
        return self._repository.list_status_history(
            incident_id,
            limit=limit,
            offset=offset,
        )

    def change_status(
        self,
        incident_id: str,
        status: IncidentStatus,
        *,
        expected_revision: int,
        actor: str,
        reason: str,
    ) -> Incident:
        """Change an incident's status and append history atomically."""
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 1
        ):
            raise ValueError(
                "expected_revision must be an integer greater than zero."
            )

        target_status = IncidentStatus(status)
        with self._repository.transaction() as unit_of_work:
            incident = unit_of_work.get_by_id(incident_id)
            if incident is None:
                raise IncidentNotFoundError(incident_id)

            if incident.revision != expected_revision:
                raise IncidentRevisionConflictError(
                    incident_id,
                    expected_revision,
                    incident.revision,
                )

            changed = validate_status_transition(
                incident.status,
                target_status,
            )
            if not changed:
                return incident

            changed_at = datetime.now(timezone.utc)
            status_change = IncidentStatusChange(
                change_id=str(uuid4()),
                incident_id=incident_id,
                from_status=incident.status,
                to_status=target_status,
                changed_at=changed_at,
                actor=actor,
                reason=reason,
            )
            updated_incident = replace(
                incident,
                status=target_status,
                updated_at=max(incident.updated_at, changed_at),
                resolved_at=(
                    changed_at
                    if target_status is IncidentStatus.RESOLVED
                    else None
                ),
            )
            persisted_incident = unit_of_work.update_incident(
                updated_incident,
                expected_revision=expected_revision,
            )
            unit_of_work.record_status_change(status_change)

        return persisted_incident


__all__ = ["IncidentManagementService"]
