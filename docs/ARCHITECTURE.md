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
2. Dataset analysis coordinates quality checks, statistical anomaly analysis,
   and schema handling.
3. Quality analysis evaluates data quality and produces quality issues.
   Numeric anomaly detection uses statistical analysis.
4. The schema registry stores the first observed schema as a dataset baseline.
   Later analyses compare the received schema with that baseline and report
   schema drift. A new baseline version requires an explicit approval request.
5. Quality and schema drift issues are processed as incidents. Incident data,
   occurrences, and status history are persisted in SQLite and exposed through
   the incident API, which supports filters, pagination, and status changes.

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
- SQLite is the only implemented persistence backend.
- PostgreSQL support, Docker workflows, and CI/CD are not implemented.
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
