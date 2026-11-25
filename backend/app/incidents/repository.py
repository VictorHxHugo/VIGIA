"""Persistence contracts for incident management."""

from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Protocol

from app.incidents.models import (
    Incident,
    IncidentOccurrence,
    IncidentSeverity,
    IncidentStatus,
    IncidentStatusChange,
)


class IncidentNotFoundError(Exception):
    """Raised when an operation requires an incident that does not exist."""

    def __init__(self, incident_id: str) -> None:
        self.incident_id = incident_id
        super().__init__(f"Incident '{incident_id}' was not found.")


class IncidentAlreadyExistsError(Exception):
    """Raised when a dataset already has an incident with the same fingerprint."""

    def __init__(self, dataset_id: str, fingerprint: str) -> None:
        self.dataset_id = dataset_id
        self.fingerprint = fingerprint
        super().__init__(
            f"An incident with fingerprint '{fingerprint}' already exists "
            f"for dataset '{dataset_id}'."
        )


class IncidentRevisionConflictError(Exception):
    """Raised when an update uses a revision that is no longer current."""

    def __init__(
        self,
        incident_id: str,
        expected_revision: int,
        current_revision: int,
    ) -> None:
        self.incident_id = incident_id
        self.expected_revision = expected_revision
        self.current_revision = current_revision
        super().__init__(
            f"Incident '{incident_id}' is at revision {current_revision}; "
            f"revision {expected_revision} was expected."
        )


@dataclass(frozen=True, slots=True)
class IncidentFilters:
    """Supported filters and pagination for listing incidents."""

    dataset_id: str | None = None
    status: IncidentStatus | None = None
    severity: IncidentSeverity | None = None
    code: str | None = None
    limit: int = 50
    offset: int = 0

    def __post_init__(self) -> None:
        for field_name in ("dataset_id", "code"):
            value = getattr(self, field_name)
            if value is not None and (
                not isinstance(value, str) or not value.strip()
            ):
                raise ValueError(f"{field_name} must be a non-empty string.")

        if self.status is not None:
            try:
                object.__setattr__(self, "status", IncidentStatus(self.status))
            except (TypeError, ValueError) as exc:
                raise ValueError("status is not a valid incident status.") from exc

        if self.severity is not None:
            try:
                object.__setattr__(
                    self,
                    "severity",
                    IncidentSeverity(self.severity),
                )
            except (TypeError, ValueError) as exc:
                raise ValueError("severity is not a valid incident severity.") from exc

        if (
            isinstance(self.limit, bool)
            or not isinstance(self.limit, int)
            or not 1 <= self.limit <= 100
        ):
            raise ValueError("limit must be an integer between 1 and 100.")
        if (
            isinstance(self.offset, bool)
            or not isinstance(self.offset, int)
            or self.offset < 0
        ):
            raise ValueError("offset must be a non-negative integer.")


class IncidentUnitOfWork(Protocol):
    """Incident operations sharing one database transaction."""

    def get_by_id(self, incident_id: str) -> Incident | None:
        """Return an incident by identifier, or None if absent."""
        ...

    def get_by_fingerprint(
        self,
        dataset_id: str,
        fingerprint: str,
    ) -> Incident | None:
        """Return the matching incident, or None if absent."""
        ...

    def create_incident(self, incident: Incident) -> Incident:
        """Create an incident whose initial revision is one."""
        ...

    def update_incident(
        self,
        incident: Incident,
        *,
        expected_revision: int,
    ) -> Incident:
        """Persist replacement values using an optimistic revision check."""
        ...

    def record_occurrence(self, occurrence: IncidentOccurrence) -> bool:
        """Record an occurrence; return False only for a duplicate analysis."""
        ...

    def record_status_change(self, change: IncidentStatusChange) -> None:
        """Append a status change to the incident's history."""
        ...


class IncidentRepository(Protocol):
    """Persistence operations required by incident application services."""

    def transaction(self) -> AbstractContextManager[IncidentUnitOfWork]:
        """Return a unit of work committed or rolled back as one transaction."""
        ...

    def get_incident(self, incident_id: str) -> Incident | None:
        """Return an incident by identifier, or None if absent."""
        ...

    def list_incidents(
        self,
        filters: IncidentFilters,
    ) -> list[Incident]:
        """Return incidents matching filters in deterministic order."""
        ...

    def list_occurrences(
        self,
        incident_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[IncidentOccurrence]:
        """Return an incident's occurrences in deterministic order."""
        ...

    def list_status_history(
        self,
        incident_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[IncidentStatusChange]:
        """Return an incident's status history in insertion order."""
        ...
