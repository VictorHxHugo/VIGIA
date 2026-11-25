"""SQLite persistence adapter for incident management."""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from app.incidents.models import (
    Incident,
    IncidentOccurrence,
    IncidentSeverity,
    IncidentStatus,
    IncidentStatusChange,
)
from app.incidents.repository import (
    IncidentAlreadyExistsError,
    IncidentFilters,
    IncidentNotFoundError,
    IncidentRevisionConflictError,
    IncidentUnitOfWork,
)


class SQLiteIncidentUnitOfWork:
    """SQLite-backed incident operations bound to one transaction."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def get_by_id(self, incident_id: str) -> Incident | None:
        SQLiteIncidentRepository._validate_identifier(incident_id, "incident_id")
        row = self._connection.execute(
            "SELECT * FROM incidents WHERE incident_id = ?",
            (incident_id,),
        ).fetchone()
        return (
            SQLiteIncidentRepository._row_to_incident(row)
            if row is not None
            else None
        )

    def get_by_fingerprint(
        self,
        dataset_id: str,
        fingerprint: str,
    ) -> Incident | None:
        SQLiteIncidentRepository._validate_identifier(dataset_id, "dataset_id")
        SQLiteIncidentRepository._validate_identifier(fingerprint, "fingerprint")
        row = self._connection.execute(
            """
            SELECT * FROM incidents
            WHERE dataset_id = ? AND fingerprint = ?
            """,
            (dataset_id, fingerprint),
        ).fetchone()
        return (
            SQLiteIncidentRepository._row_to_incident(row)
            if row is not None
            else None
        )

    def create_incident(self, incident: Incident) -> Incident:
        if incident.revision != 1:
            raise ValueError("A new incident must have revision 1.")

        cursor = self._connection.execute(
            """
            INSERT INTO incidents (
                incident_id, dataset_id, fingerprint, code, title, description,
                column_name, severity, status, created_at, updated_at,
                first_seen_at, last_seen_at, resolved_at, occurrence_count,
                revision
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(dataset_id, fingerprint) DO NOTHING
            """,
            SQLiteIncidentRepository._incident_values(incident),
        )
        if cursor.rowcount == 0:
            raise IncidentAlreadyExistsError(
                incident.dataset_id,
                incident.fingerprint,
            )
        return incident

    def update_incident(
        self,
        incident: Incident,
        *,
        expected_revision: int,
    ) -> Incident:
        SQLiteIncidentRepository._validate_revision(expected_revision)
        if incident.revision != expected_revision:
            raise ValueError(
                "incident.revision must equal expected_revision."
            )

        result = self._connection.execute(
            """
            UPDATE incidents
            SET dataset_id = ?, fingerprint = ?, code = ?, title = ?,
                description = ?, column_name = ?, severity = ?, status = ?,
                created_at = ?, updated_at = ?, first_seen_at = ?,
                last_seen_at = ?, resolved_at = ?, occurrence_count = ?,
                revision = ?
            WHERE incident_id = ? AND revision = ?
            """,
            (
                incident.dataset_id,
                incident.fingerprint,
                incident.code,
                incident.title,
                incident.description,
                incident.column,
                incident.severity.value,
                incident.status.value,
                SQLiteIncidentRepository._datetime_to_text(incident.created_at),
                SQLiteIncidentRepository._datetime_to_text(incident.updated_at),
                SQLiteIncidentRepository._datetime_to_text(incident.first_seen_at),
                SQLiteIncidentRepository._datetime_to_text(incident.last_seen_at),
                (
                    SQLiteIncidentRepository._datetime_to_text(incident.resolved_at)
                    if incident.resolved_at is not None
                    else None
                ),
                incident.occurrence_count,
                expected_revision + 1,
                incident.incident_id,
                expected_revision,
            ),
        )
        if result.rowcount != 1:
            row = self._connection.execute(
                "SELECT revision FROM incidents WHERE incident_id = ?",
                (incident.incident_id,),
            ).fetchone()
            if row is None:
                raise IncidentNotFoundError(incident.incident_id)
            raise IncidentRevisionConflictError(
                incident.incident_id,
                expected_revision,
                row["revision"],
            )

        updated = self.get_by_id(incident.incident_id)
        if updated is None:
            raise RuntimeError("The updated incident could not be retrieved.")
        return updated

    def record_occurrence(self, occurrence: IncidentOccurrence) -> bool:
        cursor = self._connection.execute(
            """
            INSERT INTO incident_occurrences (
                occurrence_id, incident_id, analysis_id, detected_at, code,
                title, description, column_name, severity, affected_rows,
                examples_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(incident_id, analysis_id) DO NOTHING
            """,
            SQLiteIncidentRepository._occurrence_values(occurrence),
        )
        return cursor.rowcount == 1

    def record_status_change(self, change: IncidentStatusChange) -> None:
        self._connection.execute(
            """
            INSERT INTO incident_status_history (
                change_id, incident_id, from_status, to_status, changed_at,
                actor, reason
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                change.change_id,
                change.incident_id,
                change.from_status.value if change.from_status is not None else None,
                change.to_status.value,
                SQLiteIncidentRepository._datetime_to_text(change.changed_at),
                change.actor,
                change.reason,
            ),
        )


class SQLiteIncidentRepository:
    """Store incidents and their histories in a file-backed SQLite database."""

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

    @contextmanager
    def transaction(self) -> Iterator[IncidentUnitOfWork]:
        """Yield a unit of work with atomic commit and rollback semantics."""
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield SQLiteIncidentUnitOfWork(connection)
            except BaseException as original_error:
                try:
                    if connection.in_transaction:
                        connection.rollback()
                except BaseException as rollback_error:
                    raise rollback_error from original_error
                raise
            else:
                try:
                    connection.commit()
                except BaseException as commit_error:
                    try:
                        if connection.in_transaction:
                            connection.rollback()
                    except BaseException as rollback_error:
                        raise rollback_error from commit_error
                    raise

    def get_incident(self, incident_id: str) -> Incident | None:
        SQLiteIncidentRepository._validate_identifier(incident_id, "incident_id")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM incidents WHERE incident_id = ?",
                (incident_id,),
            ).fetchone()
        return (
            SQLiteIncidentRepository._row_to_incident(row)
            if row is not None
            else None
        )

    def list_incidents(self, filters: IncidentFilters) -> list[Incident]:
        if not isinstance(filters, IncidentFilters):
            raise ValueError("filters must be an IncidentFilters instance.")

        clauses: list[str] = []
        parameters: list[object] = []
        if filters.dataset_id is not None:
            clauses.append("dataset_id = ?")
            parameters.append(filters.dataset_id)
        if filters.status is not None:
            clauses.append("status = ?")
            parameters.append(filters.status.value)
        if filters.severity is not None:
            clauses.append("severity = ?")
            parameters.append(filters.severity.value)
        if filters.code is not None:
            clauses.append("code = ?")
            parameters.append(filters.code)

        where_clause = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.extend((filters.limit, filters.offset))
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM incidents
                {where_clause}
                ORDER BY updated_at DESC, incident_id ASC
                LIMIT ? OFFSET ?
                """,
                parameters,
            ).fetchall()
        return [SQLiteIncidentRepository._row_to_incident(row) for row in rows]

    def list_occurrences(
        self,
        incident_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[IncidentOccurrence]:
        self._validate_pagination(limit, offset)
        self._validate_identifier(incident_id, "incident_id")
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM incident_occurrences
                WHERE incident_id = ?
                ORDER BY detected_at DESC, occurrence_id ASC
                LIMIT ? OFFSET ?
                """,
                (incident_id, limit, offset),
            ).fetchall()
        return [self._row_to_occurrence(row) for row in rows]

    def list_status_history(
        self,
        incident_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[IncidentStatusChange]:
        self._validate_pagination(limit, offset)
        self._validate_identifier(incident_id, "incident_id")
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM incident_status_history
                WHERE incident_id = ?
                ORDER BY sequence ASC
                LIMIT ? OFFSET ?
                """,
                (incident_id, limit, offset),
            ).fetchall()
        return [self._row_to_status_change(row) for row in rows]

    def _initialize_database(self) -> None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS incidents (
                        incident_id TEXT PRIMARY KEY
                            CHECK (length(trim(incident_id)) > 0),
                        dataset_id TEXT NOT NULL
                            CHECK (length(trim(dataset_id)) > 0),
                        fingerprint TEXT NOT NULL
                            CHECK (length(trim(fingerprint)) > 0),
                        code TEXT NOT NULL CHECK (length(trim(code)) > 0),
                        title TEXT NOT NULL CHECK (length(trim(title)) > 0),
                        description TEXT NOT NULL
                            CHECK (length(trim(description)) > 0),
                        column_name TEXT,
                        severity TEXT NOT NULL
                            CHECK (severity IN ('low', 'medium', 'high', 'critical')),
                        status TEXT NOT NULL
                            CHECK (status IN ('OPEN', 'ACKNOWLEDGED', 'RESOLVED')),
                        created_at TEXT NOT NULL CHECK (length(created_at) > 0),
                        updated_at TEXT NOT NULL CHECK (length(updated_at) > 0),
                        first_seen_at TEXT NOT NULL
                            CHECK (length(first_seen_at) > 0),
                        last_seen_at TEXT NOT NULL
                            CHECK (length(last_seen_at) > 0),
                        resolved_at TEXT,
                        occurrence_count INTEGER NOT NULL
                            CHECK (occurrence_count >= 0),
                        revision INTEGER NOT NULL CHECK (revision >= 1),
                        UNIQUE (dataset_id, fingerprint)
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS incident_occurrences (
                        occurrence_id TEXT PRIMARY KEY
                            CHECK (length(trim(occurrence_id)) > 0),
                        incident_id TEXT NOT NULL
                            REFERENCES incidents(incident_id) ON DELETE CASCADE,
                        analysis_id TEXT NOT NULL
                            CHECK (length(trim(analysis_id)) > 0),
                        detected_at TEXT NOT NULL CHECK (length(detected_at) > 0),
                        code TEXT NOT NULL CHECK (length(trim(code)) > 0),
                        title TEXT NOT NULL CHECK (length(trim(title)) > 0),
                        description TEXT NOT NULL
                            CHECK (length(trim(description)) > 0),
                        column_name TEXT,
                        severity TEXT NOT NULL
                            CHECK (severity IN ('low', 'medium', 'high', 'critical')),
                        affected_rows INTEGER NOT NULL CHECK (affected_rows >= 0),
                        examples_json TEXT NOT NULL
                            CHECK (json_valid(examples_json)),
                        UNIQUE (incident_id, analysis_id)
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS incident_status_history (
                        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                        change_id TEXT NOT NULL UNIQUE
                            CHECK (length(trim(change_id)) > 0),
                        incident_id TEXT NOT NULL
                            REFERENCES incidents(incident_id) ON DELETE CASCADE,
                        from_status TEXT
                            CHECK (
                                from_status IS NULL OR
                                from_status IN ('OPEN', 'ACKNOWLEDGED', 'RESOLVED')
                            ),
                        to_status TEXT NOT NULL
                            CHECK (to_status IN ('OPEN', 'ACKNOWLEDGED', 'RESOLVED')),
                        changed_at TEXT NOT NULL CHECK (length(changed_at) > 0),
                        actor TEXT NOT NULL CHECK (length(trim(actor)) > 0),
                        reason TEXT NOT NULL CHECK (length(trim(reason)) > 0)
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS ix_incidents_dataset_status_updated
                    ON incidents (dataset_id, status, updated_at)
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS ix_occurrences_incident_detected
                    ON incident_occurrences (incident_id, detected_at)
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS ix_status_history_incident_sequence
                    ON incident_status_history (incident_id, sequence)
                    """
                )
                connection.commit()
            except BaseException as original_error:
                try:
                    if connection.in_transaction:
                        connection.rollback()
                except BaseException as rollback_error:
                    raise rollback_error from original_error
                raise

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._open_connection()
        try:
            yield connection
        finally:
            connection.close()

    def _open_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=self.busy_timeout_ms / 1000,
        )
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
        except BaseException:
            connection.close()
            raise
        return connection

    @classmethod
    def _row_to_incident(cls, row: sqlite3.Row) -> Incident:
        return Incident(
            incident_id=row["incident_id"],
            dataset_id=row["dataset_id"],
            fingerprint=row["fingerprint"],
            code=row["code"],
            title=row["title"],
            description=row["description"],
            column=row["column_name"],
            severity=IncidentSeverity(row["severity"]),
            status=IncidentStatus(row["status"]),
            created_at=cls._text_to_datetime(row["created_at"]),
            updated_at=cls._text_to_datetime(row["updated_at"]),
            first_seen_at=cls._text_to_datetime(row["first_seen_at"]),
            last_seen_at=cls._text_to_datetime(row["last_seen_at"]),
            resolved_at=(
                cls._text_to_datetime(row["resolved_at"])
                if row["resolved_at"] is not None
                else None
            ),
            occurrence_count=row["occurrence_count"],
            revision=row["revision"],
        )

    @classmethod
    def _row_to_occurrence(cls, row: sqlite3.Row) -> IncidentOccurrence:
        try:
            examples = json.loads(row["examples_json"])
        except json.JSONDecodeError as exc:
            raise ValueError("Stored occurrence examples are not valid JSON.") from exc
        if not isinstance(examples, list):
            raise ValueError("Stored occurrence examples must be a JSON array.")

        return IncidentOccurrence(
            occurrence_id=row["occurrence_id"],
            incident_id=row["incident_id"],
            analysis_id=row["analysis_id"],
            detected_at=cls._text_to_datetime(row["detected_at"]),
            code=row["code"],
            title=row["title"],
            description=row["description"],
            column=row["column_name"],
            severity=IncidentSeverity(row["severity"]),
            affected_rows=row["affected_rows"],
            examples=tuple(examples),
        )

    @classmethod
    def _row_to_status_change(cls, row: sqlite3.Row) -> IncidentStatusChange:
        return IncidentStatusChange(
            change_id=row["change_id"],
            incident_id=row["incident_id"],
            from_status=(
                IncidentStatus(row["from_status"])
                if row["from_status"] is not None
                else None
            ),
            to_status=IncidentStatus(row["to_status"]),
            changed_at=cls._text_to_datetime(row["changed_at"]),
            actor=row["actor"],
            reason=row["reason"],
        )

    @staticmethod
    def _incident_values(incident: Incident) -> tuple[object, ...]:
        return (
            incident.incident_id,
            incident.dataset_id,
            incident.fingerprint,
            incident.code,
            incident.title,
            incident.description,
            incident.column,
            incident.severity.value,
            incident.status.value,
            SQLiteIncidentRepository._datetime_to_text(incident.created_at),
            SQLiteIncidentRepository._datetime_to_text(incident.updated_at),
            SQLiteIncidentRepository._datetime_to_text(incident.first_seen_at),
            SQLiteIncidentRepository._datetime_to_text(incident.last_seen_at),
            (
                SQLiteIncidentRepository._datetime_to_text(incident.resolved_at)
                if incident.resolved_at is not None
                else None
            ),
            incident.occurrence_count,
            incident.revision,
        )

    @staticmethod
    def _occurrence_values(
        occurrence: IncidentOccurrence,
    ) -> tuple[object, ...]:
        examples_json = json.dumps(
            list(occurrence.examples),
            separators=(",", ":"),
        )
        return (
            occurrence.occurrence_id,
            occurrence.incident_id,
            occurrence.analysis_id,
            SQLiteIncidentRepository._datetime_to_text(occurrence.detected_at),
            occurrence.code,
            occurrence.title,
            occurrence.description,
            occurrence.column,
            occurrence.severity.value,
            occurrence.affected_rows,
            examples_json,
        )

    @staticmethod
    def _datetime_to_text(value: datetime) -> str:
        return value.isoformat()

    @staticmethod
    def _text_to_datetime(value: str) -> datetime:
        try:
            return datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("Stored timestamp is not valid ISO 8601.") from exc

    @staticmethod
    def _validate_identifier(value: str, field_name: str) -> None:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field_name} must be a non-empty string.")

    @staticmethod
    def _validate_revision(revision: int) -> None:
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise ValueError("expected_revision must be an integer greater than zero.")

    @staticmethod
    def _validate_pagination(limit: int, offset: int) -> None:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer between 1 and 100.")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a non-negative integer.")


__all__ = ["SQLiteIncidentRepository", "SQLiteIncidentUnitOfWork"]
