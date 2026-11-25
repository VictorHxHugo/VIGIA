"""Persistent schema registry implementations and contracts."""

from app.schema_registry.repository import (
    DatasetNotFoundError,
    Schema,
    SchemaRegistryRepository,
    SchemaVersion,
    SchemaVersionConflictError,
)
from app.schema_registry.sqlite_repository import SQLiteSchemaRegistryRepository

__all__ = [
    "DatasetNotFoundError",
    "SQLiteSchemaRegistryRepository",
    "Schema",
    "SchemaRegistryRepository",
    "SchemaVersion",
    "SchemaVersionConflictError",
]
