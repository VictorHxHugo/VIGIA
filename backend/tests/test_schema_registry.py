import json
import sqlite3
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import cast
from uuid import UUID, uuid4

import pytest

from app.schema_registry import (
    DatasetNotFoundError,
    SQLiteSchemaRegistryRepository,
    SchemaVersionConflictError,
)
from app.schema_registry.sqlite_repository import SQLiteSchemaRegistryUnitOfWork


def test_creates_initial_schema_version(tmp_path):
    database_path = tmp_path / "registry.sqlite3"
    repository = SQLiteSchemaRegistryRepository(database_path)

    schema_version, created = repository.get_or_create_initial(
        "customers",
        {"id": "int64", "name": "object"},
    )

    assert created is True
    assert schema_version.dataset_id == "customers"
    assert schema_version.version == 1
    assert schema_version.schema == {"id": "int64", "name": "object"}
    assert schema_version.approved_by_user_id is None
    assert repository.get_current("customers") == schema_version
    with closing(sqlite3.connect(database_path)) as connection:
        stored_schema = connection.execute(
            """
            SELECT schema_json
            FROM schema_versions
            WHERE dataset_id = ? AND version = 1
            """,
            ("customers",),
        ).fetchone()[0]

    assert json.loads(stored_schema) == schema_version.schema


def test_does_not_overwrite_existing_baseline(tmp_path):
    repository = SQLiteSchemaRegistryRepository(tmp_path / "registry.sqlite3")
    initial, _ = repository.get_or_create_initial(
        "customers",
        {"id": "int64"},
    )

    current, created = repository.get_or_create_initial(
        "customers",
        {"id": "object", "email": "object"},
    )

    assert created is False
    assert current == initial
    assert repository.list_versions("customers") == [initial]


def test_persists_versions_after_reopening_repository(tmp_path):
    database_path = tmp_path / "registry.sqlite3"
    first_repository = SQLiteSchemaRegistryRepository(database_path)
    expected, _ = first_repository.get_or_create_initial(
        "customers",
        {"id": "int64"},
    )

    reopened_repository = SQLiteSchemaRegistryRepository(database_path)

    assert reopened_repository.get_current("customers") == expected


def test_migrates_existing_schema_versions_and_is_idempotent(tmp_path):
    database_path = tmp_path / "legacy-registry.sqlite3"
    with closing(sqlite3.connect(database_path)) as connection, connection:
        connection.execute(
            """
            CREATE TABLE schema_versions (
                dataset_id TEXT NOT NULL
                    CHECK (length(trim(dataset_id)) > 0),
                version INTEGER NOT NULL CHECK (version >= 1),
                schema_json TEXT NOT NULL CHECK (json_valid(schema_json)),
                created_at TEXT NOT NULL CHECK (length(created_at) > 0),
                is_current INTEGER NOT NULL CHECK (is_current IN (0, 1)),
                PRIMARY KEY (dataset_id, version)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO schema_versions (
                dataset_id, version, schema_json, created_at, is_current
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                "customers",
                1,
                '{"id":"int64"}',
                "2026-01-01T00:00:00+00:00",
                1,
            ),
        )

    repository = SQLiteSchemaRegistryRepository(database_path)
    legacy_version = repository.get_current("customers")

    assert legacy_version is not None
    assert legacy_version.schema == {"id": "int64"}
    assert legacy_version.approved_by_user_id is None

    reopened_repository = SQLiteSchemaRegistryRepository(database_path)
    approved_by_user_id = uuid4()
    approved = reopened_repository.approve_schema(
        "customers",
        {"id": "int64", "name": "object"},
        expected_version=1,
        approved_by_user_id=approved_by_user_id,
    )

    assert approved.approved_by_user_id == approved_by_user_id
    versions = reopened_repository.list_versions("customers")
    assert versions[0].approved_by_user_id is None
    assert versions[1].approved_by_user_id == approved_by_user_id
    with closing(sqlite3.connect(database_path)) as connection:
        columns = [
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(schema_versions)"
            )
        ]

    assert columns.count("approved_by_user_id") == 1


def test_approval_creates_new_version_and_preserves_history(tmp_path):
    repository = SQLiteSchemaRegistryRepository(tmp_path / "registry.sqlite3")
    initial, _ = repository.get_or_create_initial(
        "customers",
        {"id": "int64"},
    )
    approved_by_user_id = uuid4()

    approved = repository.approve_schema(
        "customers",
        {"id": "int64", "name": "object"},
        expected_version=1,
        approved_by_user_id=approved_by_user_id,
    )

    assert approved.version == 2
    assert approved.schema == {"id": "int64", "name": "object"}
    assert approved.created_at
    assert approved.approved_by_user_id == approved_by_user_id
    assert repository.get_current("customers") == approved
    assert repository.list_versions("customers") == [initial, approved]


def test_stale_approval_raises_conflict_without_changing_history(tmp_path):
    repository = SQLiteSchemaRegistryRepository(tmp_path / "registry.sqlite3")
    repository.get_or_create_initial("customers", {"id": "int64"})
    repository.approve_schema(
        "customers",
        {"id": "int64", "name": "object"},
        expected_version=1,
        approved_by_user_id=uuid4(),
    )

    with pytest.raises(SchemaVersionConflictError) as error:
        repository.approve_schema(
            "customers",
            {"id": "object"},
            expected_version=1,
            approved_by_user_id=uuid4(),
        )

    assert error.value.expected_version == 1
    assert error.value.current_version == 2
    assert [version.version for version in repository.list_versions("customers")] == [
        1,
        2,
    ]


def test_approval_for_unknown_dataset_raises_not_found(tmp_path):
    repository = SQLiteSchemaRegistryRepository(tmp_path / "registry.sqlite3")

    with pytest.raises(DatasetNotFoundError):
        repository.approve_schema(
            "customers",
            {"id": "int64"},
            expected_version=1,
            approved_by_user_id=uuid4(),
        )


def test_unit_of_work_uses_external_transaction_without_managing_it(tmp_path):
    database_path = tmp_path / "registry.sqlite3"
    repository = SQLiteSchemaRegistryRepository(database_path)
    connection = sqlite3.connect(database_path)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN IMMEDIATE")
        unit_of_work = SQLiteSchemaRegistryUnitOfWork(connection)

        assert unit_of_work.get_current("customers") is None

        initial, created = unit_of_work.get_or_create_initial(
            "customers",
            {"id": "int64"},
        )
        approved = unit_of_work.approve_schema(
            "customers",
            {"id": "int64", "name": "object"},
            expected_version=1,
            approved_by_user_id=uuid4(),
        )

        assert created is True
        assert initial.version == 1
        assert unit_of_work.get_current("customers") == approved
        assert unit_of_work.list_versions("customers") == [initial, approved]
        assert connection.in_transaction is True
        assert repository.get_current("customers") is None

        connection.rollback()
    finally:
        connection.close()

    assert repository.get_current("customers") is None
    assert repository.list_versions("customers") == []


def test_unit_of_work_leaves_failed_operation_rollback_to_caller(tmp_path):
    database_path = tmp_path / "registry.sqlite3"
    repository = SQLiteSchemaRegistryRepository(database_path)
    initial, _ = repository.get_or_create_initial("customers", {"id": "int64"})
    with closing(sqlite3.connect(database_path)) as setup_connection, setup_connection:
        setup_connection.execute(
            """
            CREATE TRIGGER reject_second_version
            BEFORE INSERT ON schema_versions
            WHEN NEW.version = 2
            BEGIN
                SELECT RAISE(ABORT, 'forced insert failure');
            END
            """
        )

    connection = sqlite3.connect(database_path)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN IMMEDIATE")
        unit_of_work = SQLiteSchemaRegistryUnitOfWork(connection)

        with pytest.raises(sqlite3.IntegrityError, match="forced insert failure"):
            unit_of_work.approve_schema(
                "customers",
                {"id": "int64", "name": "object"},
                expected_version=1,
                approved_by_user_id=uuid4(),
            )

        assert connection.in_transaction is True
        assert unit_of_work.get_current("customers") is None
        connection.rollback()
    finally:
        connection.close()

    assert repository.get_current("customers") == initial


def test_unit_of_work_does_not_change_connection_row_factory(tmp_path):
    repository = SQLiteSchemaRegistryRepository(tmp_path / "registry.sqlite3")
    connection = sqlite3.connect(repository.database_path)
    original_row_factory = connection.row_factory
    try:
        SQLiteSchemaRegistryUnitOfWork(connection)

        assert connection.row_factory is original_row_factory
    finally:
        connection.close()


def test_independent_repository_validates_before_opening_connections(
    tmp_path,
    monkeypatch,
):
    repository = SQLiteSchemaRegistryRepository(tmp_path / "registry.sqlite3")

    def fail_if_connection_is_opened():
        pytest.fail("A connection was opened before input validation.")

    monkeypatch.setattr(repository, "_connection", fail_if_connection_is_opened)

    with pytest.raises(ValueError, match="dataset_id"):
        repository.get_current("")
    with pytest.raises(ValueError, match="dataset_id"):
        repository.get_or_create_initial("", {"id": "int64"})
    with pytest.raises(ValueError, match="data types"):
        repository.get_or_create_initial("customers", {"id": ""})
    with pytest.raises(ValueError, match="expected_version"):
        repository.approve_schema(
            "customers",
            {"id": "int64"},
            0,
            approved_by_user_id=uuid4(),
        )
    with pytest.raises(ValueError, match="dataset_id"):
        repository.list_versions("")


def test_approval_requires_uuid_approver(tmp_path):
    repository = SQLiteSchemaRegistryRepository(tmp_path / "registry.sqlite3")
    repository.get_or_create_initial("customers", {"id": "int64"})

    with pytest.raises(ValueError, match="approved_by_user_id must be a UUID"):
        repository.approve_schema(
            "customers",
            {"id": "int64", "name": "object"},
            expected_version=1,
            approved_by_user_id=cast(UUID, "not-a-uuid"),
        )

    assert [version.version for version in repository.list_versions("customers")] == [
        1
    ]


def test_failed_approval_rolls_back_current_version_change(tmp_path):
    database_path = tmp_path / "registry.sqlite3"
    repository = SQLiteSchemaRegistryRepository(database_path)
    initial, _ = repository.get_or_create_initial(
        "customers",
        {"id": "int64"},
    )

    with closing(sqlite3.connect(database_path)) as connection, connection:
        connection.execute(
            """
            CREATE TRIGGER reject_second_version
            BEFORE INSERT ON schema_versions
            WHEN NEW.version = 2
            BEGIN
                SELECT RAISE(ABORT, 'forced insert failure');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="forced insert failure"):
        repository.approve_schema(
            "customers",
            {"id": "int64", "name": "object"},
            expected_version=1,
            approved_by_user_id=uuid4(),
        )

    assert repository.get_current("customers") == initial
    assert repository.list_versions("customers") == [initial]


def test_database_constraints_reject_invalid_rows(tmp_path):
    database_path = tmp_path / "registry.sqlite3"
    repository = SQLiteSchemaRegistryRepository(database_path)
    repository.get_or_create_initial("customers", {"id": "int64"})

    with closing(sqlite3.connect(database_path)) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO schema_versions
                    (dataset_id, version, schema_json, created_at, is_current)
                VALUES ('', 1, '{}', '2026-01-01T00:00:00+00:00', 1)
                """
            )

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO schema_versions
                    (dataset_id, version, schema_json, created_at, is_current)
                VALUES ('customers', 0, '{}', '2026-01-01T00:00:00+00:00', 0)
                """
            )

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO schema_versions
                    (dataset_id, version, schema_json, created_at, is_current)
                VALUES ('customers', 2, '{invalid', '2026-01-01T00:00:00+00:00', 0)
                """
            )

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO schema_versions
                    (dataset_id, version, schema_json, created_at, is_current)
                VALUES ('customers', 2, '{}', '2026-01-01T00:00:00+00:00', 1)
                """
            )


def test_concurrent_initial_creation_keeps_one_baseline(tmp_path):
    repository = SQLiteSchemaRegistryRepository(
        tmp_path / "registry.sqlite3",
        busy_timeout_ms=10000,
    )
    barrier = Barrier(2)

    def register(schema):
        barrier.wait()
        return repository.get_or_create_initial("customers", schema)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(register, {"id": "int64"}),
            executor.submit(register, {"id": "object"}),
        ]
        results = [future.result() for future in futures]

    assert sorted(created for _, created in results) == [False, True]
    current = repository.get_current("customers")
    assert current is not None
    assert current.version == 1
    assert len(repository.list_versions("customers")) == 1


def test_concurrent_approvals_preserve_optimistic_version_conflicts(tmp_path):
    repository = SQLiteSchemaRegistryRepository(
        tmp_path / "registry.sqlite3",
        busy_timeout_ms=10000,
    )
    repository.get_or_create_initial("customers", {"id": "int64"})
    barrier = Barrier(2)

    def approve(schema):
        barrier.wait()
        try:
            return repository.approve_schema(
                "customers",
                schema,
                expected_version=1,
                approved_by_user_id=uuid4(),
            )
        except SchemaVersionConflictError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(approve, {"id": "int64", "name": "object"}),
            executor.submit(approve, {"id": "int64", "email": "object"}),
        ]
        results = [future.result() for future in futures]

    assert sum(
        isinstance(result, SchemaVersionConflictError) for result in results
    ) == 1
    assert sum(
        not isinstance(result, SchemaVersionConflictError) for result in results
    ) == 1
    successful_approval = repository.get_current("customers")
    assert successful_approval is not None
    assert successful_approval.approved_by_user_id is not None
    assert [version.version for version in repository.list_versions("customers")] == [
        1,
        2,
    ]


@pytest.mark.parametrize("dataset_id", ["", " customers", "customers "])
def test_rejects_invalid_dataset_id(tmp_path, dataset_id):
    repository = SQLiteSchemaRegistryRepository(tmp_path / "registry.sqlite3")

    with pytest.raises(ValueError, match="dataset_id"):
        repository.get_or_create_initial(dataset_id, {"id": "int64"})


def test_rejects_non_string_dataset_id(tmp_path):
    repository = SQLiteSchemaRegistryRepository(tmp_path / "registry.sqlite3")

    with pytest.raises(ValueError, match="dataset_id"):
        repository.get_or_create_initial(123, {"id": "int64"})


def test_rejects_non_string_schema_column_name(tmp_path):
    repository = SQLiteSchemaRegistryRepository(tmp_path / "registry.sqlite3")

    with pytest.raises(ValueError, match="column names must all be strings"):
        repository.get_or_create_initial("customers", {1: "int64"})


@pytest.mark.parametrize("expected_type", ["", "   "])
def test_rejects_empty_schema_type(tmp_path, expected_type):
    repository = SQLiteSchemaRegistryRepository(tmp_path / "registry.sqlite3")

    with pytest.raises(ValueError, match="data types must be non-empty strings"):
        repository.get_or_create_initial(
            "customers",
            {"id": expected_type},
        )


def test_rejects_non_string_schema_type(tmp_path):
    repository = SQLiteSchemaRegistryRepository(tmp_path / "registry.sqlite3")

    with pytest.raises(ValueError, match="data types must all be strings"):
        repository.get_or_create_initial("customers", {"id": 1})
