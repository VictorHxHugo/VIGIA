# VIGIA Architecture

## Current Architecture and Technology

VIGIA is a backend application for analyzing CSV datasets, detecting quality
and schema issues, and tracking those issues as incidents.

The current technology stack is:

- **Language and API:** Python and FastAPI.
- **Data processing:** Pandas.
- **Validation and data models:** Pydantic.
- **Persistence:** SQLite through Python's standard-library `sqlite3` module.
- **Automated tests:** Pytest, located in `backend/tests/`.
- **Development server:** Uvicorn.

The backend is organized into modules for ingestion, analysis, quality,
anomaly detection, schema management, and incidents. FastAPI exposes dataset
analysis, schema history and approval, health and service metadata, and
incident management endpoints.

## Implemented Processing Flow

1. The dataset analysis API accepts a CSV upload and passes it to the ingestion
   module, which validates and reads it into a Pandas DataFrame.
2. Dataset analysis builds the quality report and derives the current schema.
   Numeric anomaly detection is part of the quality analysis.
3. `DatasetAnalysisService` opens one `DatasetAnalysisRepository.transaction()`
   and uses its `DatasetAnalysisUnitOfWork` for the persistence-dependent part
   of the analysis.
4. Through `unit_of_work.schemas`, the service creates or retrieves the schema
   baseline. It compares the upload with that baseline to detect drift, then
   combines drift and quality issues.
5. Through `unit_of_work.incidents`, the incident service creates or updates
   incidents and records occurrences and status history. On success, the
   aggregate commits the transaction. An exception during drift detection or
   incident processing causes the aggregate to roll back the analysis writes.
6. The API returns the quality report, drift issues, baseline information, and
   incident-processing counts.

## Atomic Dataset Analysis

`DatasetAnalysisRepository` defines the transaction boundary for one analysis.
Its `transaction()` yields a `DatasetAnalysisUnitOfWork` with two interfaces:
`schemas` implements `SchemaRegistryUnitOfWork`, and `incidents` implements
`IncidentUnitOfWork`. The dataset service passes these UoWs to the schema and
incident services; those services do not start, commit, or roll back a
transaction in this path.

`SQLiteDatasetAnalysisRepository` opens a single SQLite connection for the
analysis, configures its row factory and required PRAGMAs, and executes
`BEGIN IMMEDIATE`. It constructs `SQLiteSchemaRegistryUnitOfWork` and
`SQLiteIncidentUnitOfWork` with that exact connection. The aggregate commits
after a successful context body. If `BEGIN IMMEDIATE` fails, it attempts
rollback if a transaction is still active and calls `close()` in the `finally`
block. If the context body or commit fails, it likewise attempts rollback only
while a transaction remains active; it does not guarantee rollback in every
failure scenario. The code calls `close()` in `finally` on every path, but
successful closure is not guaranteed if `close()` itself raises an exception.
Schema and incident repositories initialize their own tables when constructed;
the aggregate reuses those tables and does not repeat their DDL per analysis.

Baseline creation, drift detection based on that baseline, and incident,
occurrence, and status-history writes all participate in this transaction.
If a later operation fails, the aggregate attempts rollback while a transaction
is still active. The analysis writes are atomic only if that rollback succeeds;
if rollback fails, partial writes may remain.

The services retain their independent operations:

- `SchemaRegistryService.get_or_create_baseline()` uses the schema repository's
  independent operation. `get_or_create_baseline_in()` instead requires an
  external `SchemaRegistryUnitOfWork` and performs no transaction management.
- `IncidentService.process_issues()` uses the incident repository's independent
  transaction when preparation produces issues to process. That transaction is
  already open even if occurrence deduplication later discards all of them. If
  preparation produces no issues, the method can return without opening a
  transaction. `process_issues_in()` requires an external `IncidentUnitOfWork`
  and shares its business rules without opening or closing a transaction.

### Application composition

`create_app()` constructs the schema and incident SQLite repositories against
the same configured database path, then creates one
`SQLiteDatasetAnalysisRepository` with their path and busy timeout. This
aggregate is constructed once with the application, not once per request.
When a matching pair of SQLite repositories is injected, `create_app()` can
construct the aggregate for them or accept it through the optional
`dataset_analysis_repository` argument. `SQLiteDatasetAnalysisRepository`
validates that repository paths and busy timeouts match when it is constructed.
If a SQLite aggregate is injected, `create_app()` also checks those values
against the repositories. It does not verify equivalent settings or transaction
guarantees for a custom aggregate.

Custom schema and incident repositories must be supplied together. They must
also be accompanied by a custom `DatasetAnalysisRepository` that provides
atomic shared-transaction behavior for their backend. There is no fallback to
the two independent repository transactions when such an aggregate is absent.
The custom aggregate's implementer is responsible for ensuring the schema and
incident UoWs use the same transaction; `create_app()` cannot infer that
backend-specific guarantee from the general repository Protocols.

The incident API's status-change request uses revision checking. The API does
not currently authenticate users; it supplies the internal actor label
`api`.

## Repository Structure

The tree below is representative, not an exhaustive listing of every file.

```text
.
├── README.md
├── backend/
│   ├── app/
│   │   ├── anomaly/
│   │   ├── core/
│   │   │   └── __init__.py
│   │   ├── datasets/
│   │   ├── incidents/
│   │   ├── ingestion/
│   │   ├── quality/
│   │   ├── schema_drift/
│   │   ├── schema_registry/
│   │   ├── samples/
│   │   │   └── customers.csv
│   │   └── main.py
│   ├── data/                 # Local persistence directory
│   ├── requirements.txt
│   └── tests/                # Pytest suite
│       └── .gitkeep
├── docs/
│   ├── .gitkeep
│   ├── ARCHITECTURE.md
│   ├── API.md
│   └── DEVELOPMENT.md
├── frontend/                 # Placeholder; no application implemented
│   └── .gitkeep
└── pytest.ini
```

`backend/app/core/` currently contains only `__init__.py`; it does not yet
provide shared configuration or application services. `backend/data/` is the
local persistence directory, not a checked-in database requirement.

## Existing Components

- **`backend/app/main.py`** creates the FastAPI application and defines its
  HTTP API routes.
- **`backend/app/ingestion/`** reads and validates CSV uploads.
- **`backend/app/datasets/`** coordinates dataset analysis.
- **`backend/app/quality/`** contains data quality rules, analysis, and report
  schemas.
- **`backend/app/anomaly/`** performs statistical anomaly analysis.
- **`backend/app/schema_drift/`** compares DataFrame columns and Pandas dtypes
  with an expected schema.
- **`backend/app/schema_registry/`** manages dataset schema baselines and
  versions, including SQLite persistence.
- **`backend/app/incidents/`** contains incident persistence, schemas/DTOs,
  and incident management logic.
- **`backend/tests/`** contains the Pytest suite for the backend.

## Current Limitations

- `frontend/` is a placeholder; no frontend application is implemented.
- Authentication and authorization are not implemented.
- SQLite is the only implemented persistence backend. The aggregate
  transaction currently has a SQLite implementation; PostgreSQL is not
  implemented.
- Docker workflows and CI/CD are not implemented.
- Stale-dataset detection is not implemented.

## Roadmap

The following items are not implemented:

- Add authentication and authorization.
- Build a frontend application for exploring analysis results and managing
  incidents.
- Evaluate and implement PostgreSQL support if needed.
- Add Docker-based development or deployment workflows.
- Configure CI workflows for automated checks.
- Explore dataset freshness monitoring and stale-dataset detection.

For endpoint contracts, see [API.md](API.md). For local setup and contribution
guidance, see [DEVELOPMENT.md](DEVELOPMENT.md).
