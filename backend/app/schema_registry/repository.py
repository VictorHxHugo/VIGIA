"""Domain types and persistence contract for registered dataset schemas."""

from dataclasses import dataclass
from typing import Protocol, TypeAlias


Schema: TypeAlias = dict[str, str]


@dataclass(frozen=True)
class SchemaVersion:
    """A stored schema version for a dataset."""

    dataset_id: str
    version: int
    schema: Schema
    created_at: str


class DatasetNotFoundError(Exception):
    """Raised when an operation requires a dataset with a registered schema."""

    def __init__(self, dataset_id: str) -> None:
        self.dataset_id = dataset_id
        super().__init__(f"No schema is registered for dataset '{dataset_id}'.")


class SchemaVersionConflictError(Exception):
    """Raised when an approval uses a stale expected schema version."""

    def __init__(
        self,
        dataset_id: str,
        expected_version: int,
        current_version: int,
    ) -> None:
        self.dataset_id = dataset_id
        self.expected_version = expected_version
        self.current_version = current_version
        super().__init__(
            f"Dataset '{dataset_id}' is at schema version {current_version}; "
            f"version {expected_version} was expected."
        )


class SchemaRegistryUnitOfWork(Protocol):
    """Schema registry operations bound to an externally managed transaction."""

    def get_current(self, dataset_id: str) -> SchemaVersion | None:
        """Return the current schema version, or None when not registered."""
        ...

    def get_or_create_initial(
        self,
        dataset_id: str,
        schema: Schema,
    ) -> tuple[SchemaVersion, bool]:
        """Return the current version or create version one."""
        ...

    def approve_schema(
        self,
        dataset_id: str,
        schema: Schema,
        expected_version: int,
    ) -> SchemaVersion:
        """Create a version if expected_version is still current."""
        ...

    def list_versions(self, dataset_id: str) -> list[SchemaVersion]:
        """Return all schema versions in ascending version order."""
        ...


class SchemaRegistryRepository(Protocol):
    """Persistence operations required by the schema registry service."""

    def get_current(self, dataset_id: str) -> SchemaVersion | None:
        """Return the current schema version, or None when not registered."""
        ...

    def get_or_create_initial(
        self,
        dataset_id: str,
        schema: Schema,
    ) -> tuple[SchemaVersion, bool]:
        """Atomically return the current version or create version one."""
        ...

    def approve_schema(
        self,
        dataset_id: str,
        schema: Schema,
        expected_version: int,
    ) -> SchemaVersion:
        """Create a version if expected_version is still current."""
        ...

    def list_versions(self, dataset_id: str) -> list[SchemaVersion]:
        """Return all schema versions in ascending version order."""
        ...
