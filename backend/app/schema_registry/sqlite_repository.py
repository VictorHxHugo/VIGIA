"""SQLite persistence adapter for the schema registry."""

import json
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from app.schema_registry.repository import (
    DatasetNotFoundError,
    Schema,
    SchemaVersion,
    SchemaVersionConflictError,
)


class SQLiteSchemaRegistryRepository:
    """Store schema versions in a file-backed SQLite database."""

    def __init__(
        self,
        database_path: str | Path,
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
                "A file-backed database path is required because connections "
                "are opened per operation."
            )

        self.busy_timeout_ms = busy_timeout_ms
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize_database()

    def get_current(self, dataset_id: str) -> SchemaVersion | None:
        """Return the current schema version for a dataset, if present."""
        self._validate_dataset_id(dataset_id)

        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT dataset_id, version, schema_json, created_at
                FROM schema_versions
                WHERE dataset_id = ? AND is_current = 1
                """,
                (dataset_id,),
            ).fetchone()

        return self._row_to_schema_version(row) if row is not None else None

    def get_or_create_initial(
        self,
        dataset_id: str,
        schema: Schema,
    ) -> tuple[SchemaVersion, bool]:
        """Return the baseline or atomically create it as version one."""
        self._validate_dataset_id(dataset_id)
        schema_json = self._serialize_schema(schema)

        with self._write_transaction() as connection:
            row = self._get_current_row(connection, dataset_id)
            if row is not None:
                return self._row_to_schema_version(row), False

            created_at = self._utc_now()
            connection.execute(
                """
                INSERT INTO schema_versions (
                    dataset_id,
                    version,
                    schema_json,
                    created_at,
                    is_current
                )
                VALUES (?, 1, ?, ?, 1)
                """,
                (dataset_id, schema_json, created_at),
            )
            row = self._get_current_row(connection, dataset_id)
            if row is None:
                raise RuntimeError("The initial schema version was not stored.")

            return self._row_to_schema_version(row), True

    def approve_schema(
        self,
        dataset_id: str,
        schema: Schema,
        expected_version: int,
    ) -> SchemaVersion:
        """Atomically append a schema version when the expected version is current."""
        self._validate_dataset_id(dataset_id)
        self._validate_expected_version(expected_version)
        schema_json = self._serialize_schema(schema)

        with self._write_transaction() as connection:
            current_row = self._get_current_row(connection, dataset_id)
            if current_row is None:
                raise DatasetNotFoundError(dataset_id)

            current = self._row_to_schema_version(current_row)
            if current.version != expected_version:
                raise SchemaVersionConflictError(
                    dataset_id,
                    expected_version,
                    current.version,
                )

            next_version = current.version + 1
            result = connection.execute(
                """
                UPDATE schema_versions
                SET is_current = 0
                WHERE dataset_id = ? AND version = ? AND is_current = 1
                """,
                (dataset_id, current.version),
            )
            if result.rowcount != 1:
                raise SchemaVersionConflictError(
                    dataset_id,
                    expected_version,
                    current.version,
                )

            created_at = self._utc_now()
            connection.execute(
                """
                INSERT INTO schema_versions (
                    dataset_id,
                    version,
                    schema_json,
                    created_at,
                    is_current
                )
                VALUES (?, ?, ?, ?, 1)
                """,
                (dataset_id, next_version, schema_json, created_at),
            )
            row = self._get_current_row(connection, dataset_id)
            if row is None:
                raise RuntimeError("The approved schema version was not stored.")

            return self._row_to_schema_version(row)

    def list_versions(self, dataset_id: str) -> list[SchemaVersion]:
        """Return a dataset's schema history in ascending version order."""
        self._validate_dataset_id(dataset_id)

        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT dataset_id, version, schema_json, created_at
                FROM schema_versions
                WHERE dataset_id = ?
                ORDER BY version ASC
                """,
                (dataset_id,),
            ).fetchall()

        return [self._row_to_schema_version(row) for row in rows]

    def _initialize_database(self) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_versions (
                    dataset_id TEXT NOT NULL
                        CHECK (length(trim(dataset_id)) > 0),
                    version INTEGER NOT NULL
                        CHECK (version >= 1),
                    schema_json TEXT NOT NULL
                        CHECK (json_valid(schema_json)),
                    created_at TEXT NOT NULL
                        CHECK (length(created_at) > 0),
                    is_current INTEGER NOT NULL
                        CHECK (is_current IN (0, 1)),
                    PRIMARY KEY (dataset_id, version)
                )
                """
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS
                    uq_schema_versions_one_current
                ON schema_versions (dataset_id)
                WHERE is_current = 1
                """
            )
            connection.commit()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            self.database_path,
            timeout=self.busy_timeout_ms / 1000,
        )
        try:
            connection.row_factory = sqlite3.Row
            connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.commit()
            except Exception:
                if connection.in_transaction:
                    connection.rollback()
                raise

    @staticmethod
    def _get_current_row(
        connection: sqlite3.Connection,
        dataset_id: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            """
            SELECT dataset_id, version, schema_json, created_at
            FROM schema_versions
            WHERE dataset_id = ? AND is_current = 1
            """,
            (dataset_id,),
        ).fetchone()

    @classmethod
    def _row_to_schema_version(
        cls,
        row: sqlite3.Row,
    ) -> SchemaVersion:
        try:
            schema = json.loads(row["schema_json"])
        except json.JSONDecodeError as exc:
            raise ValueError("Stored schema is not valid JSON.") from exc

        cls._validate_schema(schema)
        return SchemaVersion(
            dataset_id=row["dataset_id"],
            version=row["version"],
            schema=schema,
            created_at=row["created_at"],
        )

    @staticmethod
    def _validate_dataset_id(dataset_id: str) -> None:
        if not isinstance(dataset_id, str) or not dataset_id:
            raise ValueError("dataset_id must be a non-empty string.")
        if dataset_id != dataset_id.strip():
            raise ValueError("dataset_id must not have surrounding whitespace.")

    @classmethod
    def _serialize_schema(cls, schema: Schema) -> str:
        cls._validate_schema(schema)
        return json.dumps(
            dict(schema),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )

    @staticmethod
    def _validate_schema(schema: object) -> None:
        if not isinstance(schema, Mapping):
            raise ValueError("schema must be a mapping of column names to types.")

        for column_name, expected_type in schema.items():
            if not isinstance(column_name, str):
                raise ValueError("Schema column names must all be strings.")
            if not isinstance(expected_type, str):
                raise ValueError("Schema data types must all be strings.")
            if not expected_type.strip():
                raise ValueError("Schema data types must be non-empty strings.")

    @staticmethod
    def _validate_expected_version(expected_version: int) -> None:
        if (
            isinstance(expected_version, bool)
            or not isinstance(expected_version, int)
            or expected_version < 1
        ):
            raise ValueError("expected_version must be an integer greater than zero.")

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).isoformat()
