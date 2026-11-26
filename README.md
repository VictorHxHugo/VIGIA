# VIGIA

**Data quality, schema change, and incident visibility for CSV datasets.**

VIGIA is a backend project for detecting data problems early and making them
traceable. It analyzes uploaded CSV datasets, compares their structure with a
registered baseline, and records detected issues as incidents with occurrences
and status history.

## Key Features

- **Data Quality** — profiles columns and reports missing values and duplicate
  rows, with a quality score.
- **Statistical Anomaly Detection** — identifies numeric outliers using
  interquartile range (IQR) bounds.
- **Schema Drift** — detects added or removed columns and changes to Pandas
  dtypes compared with a dataset's registered baseline.
- **Schema Registry** — creates a dataset baseline on its first analysis and
  keeps version history. A new schema version requires explicit approval.
- **Incident Management** — converts quality and schema drift issues into
  incidents, records analysis occurrences and status history, and supports
  revision-checked status changes.

## Architecture Overview

The backend processes an analysis through these stages:

1. `POST /datasets/analyze` validates and reads a CSV upload into a Pandas
   DataFrame.
2. The quality engine builds column profiles, detects missing values, duplicate
   rows, and numeric anomalies, and calculates a quality score.
3. VIGIA derives the column schema from Pandas dtypes. On the first analysis,
   it stores this as the dataset baseline; on later analyses, it detects drift
   against that baseline.
4. Quality and drift issues are sent to the incident service. Incidents are
   deduplicated by dataset, issue code, and column; analysis occurrences and
   status changes are stored separately.
5. The API returns the quality report, drift issues, baseline information, and
   incident-processing counts.

For an analysis, baseline persistence and incident writes share one SQLite
transaction; see [the architecture guide](docs/ARCHITECTURE.md) for details.

The schema registry and incident repositories use SQLite. By default, they
share `backend/data/schema_registry.sqlite3`; set `VIGIA_DATABASE_PATH` to use
another database file.

## Tech Stack

- Python
- FastAPI
- Pandas
- Pydantic
- SQLite (Python's standard-library `sqlite3`)
- Pytest
- Uvicorn

## Project Structure

The tree below is representative, not an exhaustive listing of every file.

```text
.
├── README.md
├── backend/
│   ├── app/
│   │   ├── anomaly/          # Numeric anomaly detection
│   │   ├── core/             # Minimal package scaffold
│   │   ├── datasets/         # Analysis orchestration
│   │   ├── incidents/        # Incident domain, services, API schemas, SQLite
│   │   ├── ingestion/        # CSV reading and validation
│   │   ├── quality/          # Quality rules, engine, and report schemas
│   │   ├── schema_drift/     # DataFrame-to-baseline comparisons
│   │   ├── schema_registry/  # Schema versioning and SQLite persistence
│   │   ├── samples/
│   │   │   └── customers.csv
│   │   └── main.py           # FastAPI application and routes
│   ├── data/                 # Local persistence directory
│   ├── requirements.txt
│   └── tests/                # Pytest suite
│       └── .gitkeep
├── docs/
│   ├── .gitkeep
│   ├── ARCHITECTURE.md
│   ├── API.md
│   └── DEVELOPMENT.md
├── frontend/                 # Placeholder; no frontend implementation yet
│   └── .gitkeep
└── pytest.ini
```

## Getting Started

### Requirements

- Python compatible with the pinned packages in `backend/requirements.txt`
- A shell with support for Python virtual environments

The repository does not currently declare a supported Python version range.

### Create and activate a virtual environment

**Windows PowerShell**

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
```

**Unix-like shells**

```sh
python3 -m venv .venv
source .venv/bin/activate
```

### Install dependencies

**Windows PowerShell**

```powershell
python -m pip install -r backend\requirements.txt
```

**Unix-like shells**

```sh
python -m pip install -r backend/requirements.txt
```

### Run the API

From the repository root, start the development server:

```sh
uvicorn app.main:app --app-dir backend --reload
```

The health endpoint is available at
[`http://127.0.0.1:8000/health`](http://127.0.0.1:8000/health) and returns a
`healthy` status when the application is running.

To select a database file, set `VIGIA_DATABASE_PATH` before starting the
application. If it is unset, VIGIA uses `backend/data/schema_registry.sqlite3`.

## API Overview

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/` | Return static service metadata |
| `GET` | `/health` | Check application health |
| `POST` | `/datasets/analyze` | Upload and analyze a CSV dataset |
| `PUT` | `/datasets/{dataset_id}/schema` | Explicitly approve a schema version |
| `GET` | `/datasets/{dataset_id}/schema/history` | List registered schema versions |
| `GET` | `/incidents` | List incidents with filters and pagination |
| `GET` | `/incidents/{incident_id}` | Retrieve an incident |
| `GET` | `/incidents/{incident_id}/occurrences` | List incident occurrences |
| `GET` | `/incidents/{incident_id}/history` | List status changes |
| `PATCH` | `/incidents/{incident_id}/status` | Change status using an expected revision |

`GET /` returns static service metadata; `/health` is the health check.

`GET /datasets/{dataset_id}/schema/history` returns `dataset_id` and `versions`.
It can return HTTP 404 when no schema versions are registered.

`POST /datasets/analyze` accepts a multipart CSV upload and requires the
`Idempotency-Key` header. The value is passed to the analysis as `analysis_id`
and is used by incident processing to identify duplicate occurrences. It does
not cache or replay an analysis response, prevent the analysis from running
again, or bind the key to a request payload. Empty keys and keys with leading
or trailing whitespace are rejected with HTTP 422.

`GET /incidents` supports the `dataset_id`, `status`, `severity`, and `code`
filters. Incident, occurrence, and status-history list endpoints support
pagination with `limit` (1–100, default 50) and `offset` (non-negative,
default 0). Collection responses include `items`, `limit`, and `offset`; they
do not include a total count.

`PATCH /incidents/{incident_id}/status` requires `status` (a valid
`IncidentStatus`), `expected_revision` (a strict integer greater than or equal
to 1), and a `reason` that is not empty or whitespace-only. Extra fields,
including `actor`, are rejected; the API sets `actor="api"` internally.
Nonexistent incidents return 404. Stale revisions and invalid transitions
return 409. Invalid parameters or request bodies return 422, and unexpected
failures return 500. Repeating the current status is a no-op: it does not
increment the revision or add a history record.

## Running Tests

From the repository root, run:

```sh
python -m pytest
```

`pytest.ini` configures `backend` as the Python import path and
`backend/tests` as the test directory. The most recently reported full-suite
run passed **239 tests**. This is a historical result, not a guarantee for every
checkout or environment.

## Current Limitations

- The `frontend/` directory is only a placeholder; no frontend is implemented.
- The API has no authentication or authorization. In particular, incident
  status changes must not be exposed publicly without access control.
- Status-change history currently records the internal actor label `api`, not
  an authenticated user's identity.
- SQLite is the current persistence backend; PostgreSQL is not implemented.
- Docker and CI/CD workflows are not configured.
- The collection APIs do not provide total-count pagination metadata.
- Dataset freshness or stale-dataset detection is not implemented.

## Roadmap

The following items are **planned**, not implemented:

- Add authentication and authorization for administrative operations.
- Build a frontend for exploring quality results and managing incidents.
- Evaluate PostgreSQL support and deployment/backup requirements.
- Add Docker-based development or deployment workflows.
- Add CI workflows for automated checks.
- Explore dataset freshness monitoring.

## Documentation

- [Architecture](docs/ARCHITECTURE.md)
- [API reference](docs/API.md)
- [Development guide](docs/DEVELOPMENT.md)
