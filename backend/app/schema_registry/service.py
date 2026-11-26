"""Application service for schema registry operations."""

from app.schema_registry.repository import (
    Schema,
    SchemaRegistryRepository,
    SchemaRegistryUnitOfWork,
    SchemaVersion,
)


class SchemaRegistryService:
    """Coordinate schema registry use cases through a repository contract."""

    def __init__(self, repository: SchemaRegistryRepository) -> None:
        self._repository = repository

    def get_or_create_baseline(
        self,
        dataset_id: str,
        schema: Schema,
    ) -> tuple[SchemaVersion, bool]:
        """Return the dataset baseline, creating version one when absent."""
        return self._repository.get_or_create_initial(dataset_id, schema)

    def get_or_create_baseline_in(
        self,
        unit_of_work: SchemaRegistryUnitOfWork,
        dataset_id: str,
        schema: Schema,
    ) -> tuple[SchemaVersion, bool]:
        """Return or create a baseline using a caller-managed transaction."""
        return unit_of_work.get_or_create_initial(dataset_id, schema)

    def approve_schema(
        self,
        dataset_id: str,
        schema: Schema,
        expected_version: int,
    ) -> SchemaVersion:
        """Approve a new schema version if the expected version is current."""
        return self._repository.approve_schema(
            dataset_id,
            schema,
            expected_version,
        )

    def list_versions(self, dataset_id: str) -> list[SchemaVersion]:
        """Return the version history for a dataset."""
        return self._repository.list_versions(dataset_id)
