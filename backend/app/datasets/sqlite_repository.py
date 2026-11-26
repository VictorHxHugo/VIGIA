"""SQLite persistence adapter for atomic dataset analysis."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from app.datasets.repository import DatasetAnalysisUnitOfWork
from app.incidents.repository import IncidentUnitOfWork
from app.incidents.sqlite_repository import (
    SQLiteIncidentRepository,
    SQLiteIncidentUnitOfWork,
)
from app.schema_registry.repository import SchemaRegistryUnitOfWork
from app.schema_registry.sqlite_repository import (
    SQLiteSchemaRegistryRepository,
    SQLiteSchemaRegistryUnitOfWork,
)


@dataclass(frozen=True, slots=True)
class SQLiteDatasetAnalysisUnitOfWork(DatasetAnalysisUnitOfWork):
    """Schema and incident operations bound to the same SQLite connection."""

    schemas: SchemaRegistryUnitOfWork
    incidents: IncidentUnitOfWork


class SQLiteDatasetAnalysisRepository:
    """Manage the single SQLite transaction for dataset analysis."""

    def __init__(
        self,
        database_path: str | Path,
        schema_repository: SQLiteSchemaRegistryRepository,
        incident_repository: SQLiteIncidentRepository,
        busy_timeout_ms: int = 5000,
    ) -> None:
        if (
            isinstance(busy_timeout_ms, bool)
            or not isinstance(busy_timeout_ms, int)
            or busy_timeout_ms < 0
        ):
            raise ValueError("busy_timeout_ms must be a non-negative integer.")

        self.database_path = str(database_path)
        if self.database_path == ":memory:":
            raise ValueError(
                "A file-backed database path is required because the "
                "repositories use independent connections."
            )
        self.busy_timeout_ms = busy_timeout_ms

        expected_path = self._normalize_path(self.database_path)
        if self._normalize_path(schema_repository.database_path) != expected_path:
            raise ValueError(
                "schema_repository.database_path must match database_path."
            )
        if self._normalize_path(incident_repository.database_path) != expected_path:
            raise ValueError(
                "incident_repository.database_path must match database_path."
            )
        if schema_repository.busy_timeout_ms != busy_timeout_ms:
            raise ValueError(
                "schema_repository.busy_timeout_ms must match busy_timeout_ms."
            )
        if incident_repository.busy_timeout_ms != busy_timeout_ms:
            raise ValueError(
                "incident_repository.busy_timeout_ms must match busy_timeout_ms."
            )

    @contextmanager
    def transaction(self) -> Iterator[SQLiteDatasetAnalysisUnitOfWork]:
        """Yield both repository UoWs on one connection and one transaction."""
        connection = sqlite3.connect(
            self.database_path,
            timeout=self.busy_timeout_ms / 1000,
        )
        pending_error: BaseException | None = None
        try:
            connection.row_factory = sqlite3.Row
            connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
            connection.execute("PRAGMA foreign_keys = ON")

            try:
                connection.execute("BEGIN IMMEDIATE")
            except BaseException as begin_error:
                self._rollback_after_error(connection, begin_error)
                raise

            unit_of_work = SQLiteDatasetAnalysisUnitOfWork(
                schemas=SQLiteSchemaRegistryUnitOfWork(connection),
                incidents=SQLiteIncidentUnitOfWork(connection),
            )
            try:
                yield unit_of_work
            except BaseException as body_error:
                self._rollback_after_error(connection, body_error)
                raise
            else:
                try:
                    connection.commit()
                except BaseException as commit_error:
                    self._rollback_after_error(connection, commit_error)
                    raise
        except BaseException as error:
            pending_error = error
            raise
        finally:
            try:
                connection.close()
            except BaseException as close_error:
                if pending_error is not None:
                    raise close_error from pending_error
                raise

    @staticmethod
    def _normalize_path(database_path: str | Path) -> Path:
        return Path(database_path).resolve()

    @staticmethod
    def _rollback_after_error(
        connection: sqlite3.Connection,
        original_error: BaseException,
    ) -> None:
        if not connection.in_transaction:
            return

        try:
            connection.rollback()
        except BaseException as rollback_error:
            raise rollback_error from original_error
