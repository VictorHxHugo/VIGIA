"""Pydantic request and response models for the incident management API."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.incidents.models import (
    IncidentSeverity,
    IncidentStatus,
)


class IncidentResponse(BaseModel):
    """Public representation of an incident."""

    model_config = ConfigDict(from_attributes=True)

    incident_id: str
    dataset_id: str
    fingerprint: str
    code: str
    title: str
    description: str
    column: str | None
    severity: IncidentSeverity
    status: IncidentStatus
    created_at: datetime
    updated_at: datetime
    first_seen_at: datetime
    last_seen_at: datetime
    resolved_at: datetime | None
    occurrence_count: int
    revision: int


class IncidentOccurrenceResponse(BaseModel):
    """Public representation of one incident occurrence."""

    model_config = ConfigDict(from_attributes=True)

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
    examples: list[int]


class IncidentStatusHistoryResponse(BaseModel):
    """Public representation of one incident status change."""

    model_config = ConfigDict(from_attributes=True)

    change_id: str
    incident_id: str
    from_status: IncidentStatus | None
    to_status: IncidentStatus
    changed_at: datetime
    actor: str
    reason: str


class IncidentCollectionResponse(BaseModel):
    """Paginated incident response without a total count."""

    items: list[IncidentResponse]
    limit: int
    offset: int


class IncidentOccurrenceCollectionResponse(BaseModel):
    """Paginated occurrence response without a total count."""

    items: list[IncidentOccurrenceResponse]
    limit: int
    offset: int


class IncidentStatusHistoryCollectionResponse(BaseModel):
    """Paginated status history response without a total count."""

    items: list[IncidentStatusHistoryResponse]
    limit: int
    offset: int


class IncidentStatusChangeRequest(BaseModel):
    """Request body for an optimistic incident status change."""

    model_config = ConfigDict(extra="forbid")

    status: IncidentStatus
    expected_revision: int = Field(strict=True, ge=1)
    reason: str

    @field_validator("reason")
    @classmethod
    def reason_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reason must be a non-empty string.")
        return value
