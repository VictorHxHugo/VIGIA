"""Persistence contracts for atomic dataset analysis."""

from contextlib import AbstractContextManager
from typing import Protocol

from app.incidents.repository import IncidentUnitOfWork
from app.schema_registry.repository import SchemaRegistryUnitOfWork


class DatasetAnalysisUnitOfWork(Protocol):
    """Schema and incident operations sharing one transaction."""

    schemas: SchemaRegistryUnitOfWork
    incidents: IncidentUnitOfWork


class DatasetAnalysisRepository(Protocol):
    """Persistence boundary for an atomic dataset analysis."""

    def transaction(self) -> AbstractContextManager[DatasetAnalysisUnitOfWork]:
        """Return a unit of work committed or rolled back as one transaction."""
        ...
