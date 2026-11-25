"""Incident management domain models."""

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
from app.incidents.repository import (
    IncidentAlreadyExistsError,
    IncidentFilters,
    IncidentNotFoundError,
    IncidentRepository,
    IncidentRevisionConflictError,
    IncidentUnitOfWork,
)
from app.incidents.sqlite_repository import SQLiteIncidentRepository
from app.incidents.service import IncidentProcessingResult, IncidentService

__all__ = [
    "Incident",
    "IncidentAlreadyExistsError",
    "IncidentFilters",
    "IncidentNotFoundError",
    "IncidentOccurrence",
    "IncidentProcessingResult",
    "IncidentRepository",
    "IncidentRevisionConflictError",
    "IncidentSeverity",
    "IncidentStatus",
    "IncidentStatusChange",
    "IncidentService",
    "IncidentUnitOfWork",
    "InvalidIncidentTransitionError",
    "SQLiteIncidentRepository",
    "generate_incident_fingerprint",
    "validate_status_transition",
]