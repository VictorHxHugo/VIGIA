# VIGIA Development Guide

This guide covers setting up the current Python backend, running its tests,
and making changes in line with the repository's existing structure. It
describes the implementation as it exists today; it does not assume a
particular deployment platform.

## Prerequisites

- Python installed and available from your shell.
- A Python version compatible with the pinned packages in
  `backend/requirements.txt`.
- Git, if you are working from a local clone.

The project does not currently declare an official supported Python version
range. Consult the dependency requirements and your target environment when
choosing an interpreter.

## Set up a virtual environment

Run these commands from the repository root.

### Windows PowerShell

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r backend\requirements.txt
```

### Unix-like systems

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r backend/requirements.txt
```

The dependencies are pinned in `backend/requirements.txt`. There is no separate
backend package-install or project-specific setup script.

## Run the API

With the virtual environment active, run this from the repository root:

```sh
uvicorn app.main:app --app-dir backend --reload
```

The API listens at `http://127.0.0.1:8000` by default. `GET /health` reports
the current health status. See [API Reference](API.md) for routes and request
contracts.

## Configure SQLite persistence

The application reads `VIGIA_DATABASE_PATH` while `create_app()` constructs the
repositories. Set it before starting the server process.

**Windows PowerShell**

```powershell
$env:VIGIA_DATABASE_PATH = "C:\path\to\vigia.sqlite3"
uvicorn app.main:app --app-dir backend --reload
```

**Unix-like systems**

```sh
export VIGIA_DATABASE_PATH="/path/to/vigia.sqlite3"
uvicorn app.main:app --app-dir backend --reload
```

If unset, the application uses `backend/data/schema_registry.sqlite3`. The
schema registry and incident repositories share the configured database file.
Use a writable file path; an empty environment-variable value is rejected, and
the SQLite adapters do not support `:memory:` because they open connections
per operation. Relative paths are resolved from the server process's current
working directory, so an absolute path avoids ambiguity.

The adapters create the database's parent directory when needed. SQLite writes
use transactions; incident changes that update an incident and append history
are committed or rolled back together. The dataset-analysis flow stores or
retrieves the schema baseline before it processes incidents, so these two
operations are not one cross-repository transaction. A later incident
persistence failure can therefore occur after baseline creation.

## Run tests

`pytest.ini` sets `backend` as the Python import path and
`backend/tests` as the test directory. From the repository root, run the full
suite with:

```sh
python -m pytest
```

Run one test module by passing its path:

**Windows PowerShell**

```powershell
python -m pytest backend\tests\test_api.py
```

**Unix-like systems**

```sh
python -m pytest backend/tests/test_api.py
```

Other test modules cover CSV ingestion, dataset analysis, quality rules,
numeric anomaly detection, schema drift and the schema registry, as well as
incident models, persistence, services, and management.

API tests use a `tmp_path`-backed SQLite database. The API fixture creates the
schema-registry and incident repositories against the same temporary file, so
tests do not need a shared developer database. Follow that pattern for tests
that need persistence; keep unit tests isolated from the default local database.

## Backend organization

The backend code lives in `backend/app/`:

| Module | Responsibility |
|---|---|
| `main.py` | FastAPI application creation, repository/service wiring, route handling, and HTTP error mapping |
| `ingestion/` | CSV parsing and upload data validation |
| `quality/` | Quality report and issue schemas, quality rules, and the analysis engine |
| `anomaly/` | Statistical numeric anomaly detection |
| `schema_drift/` | Comparing DataFrame columns and Pandas dtypes with an expected schema |
| `schema_registry/` | Schema registry contracts, service operations, and SQLite persistence |
| `datasets/` | Orchestrating quality analysis, schema baseline/drift, and incident processing |
| `incidents/` | Incident domain models, repository contracts and SQLite adapter, detection and management services, and API DTOs |
| `samples/` | Sample CSV data |
| `core/` | Minimal package scaffold |

Tests live in `backend/tests/` and are organized by area, including
`test_api.py`, `test_csv_reader.py`, `test_quality_engine.py`,
`test_anomaly_detector.py`, `test_schema_drift.py`,
`test_schema_registry.py`, and incident-focused modules.

## Development conventions

- Keep HTTP concerns in the FastAPI layer, business rules in services, and
  persistence behind repository contracts and adapters.
- Reuse existing domain models, exception types, service methods, and DTOs
  instead of duplicating their behavior.
- Keep API request/response schemas separate from domain models where the
  existing API does so.
- Match nearby code's type annotations, naming, formatting, and docstring
  style.
- Add focused pytest coverage for new behavior and relevant invalid inputs.
  Prefer the existing `tmp_path` pattern when a test needs SQLite persistence.
- Update the relevant documentation when API or contributor-facing behavior
  changes.

The repository does not define a separate formatter, linter, or development
command in the inspected configuration. Use the existing test suite for
automated verification and avoid documenting unconfigured tooling as required.

## Recommended contribution workflow

1. Identify the existing module, service, repository contract, and nearby tests
   for the behavior being changed.
2. Keep the change within the appropriate layer; add or adjust dependency
   wiring in `main.py` only when the feature needs an HTTP endpoint.
3. Add focused tests alongside the related test modules. Use temporary files
   for SQLite integration and persistence tests.
4. Run the specific test module first, then run the full suite with
   `python -m pytest` when appropriate.
5. Review the API and documentation for any changed request, response,
   persistence, or error behavior.

## Current contributor-facing limitations

- SQLite is the only implemented persistence backend. PostgreSQL support is
  not present.
- Authentication and authorization are not implemented. The API's internal
  incident actor label is not an authenticated user identity.
- The frontend directory is a placeholder; no frontend is implemented.
- Docker and CI/CD workflows are not configured.
- Incident collection responses do not include total-count pagination
  metadata.
- Schema baseline persistence and incident processing are separate operations,
  not one transaction.
- No official Python support range is declared.
