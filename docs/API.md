# VIGIA REST API

This document describes the REST API currently implemented by VIGIA. The API
is served by FastAPI and currently uses SQLite for schema and incident
persistence.

## Run locally

From the repository root, install the backend dependencies and start the
development server:

```sh
python -m pip install -r backend/requirements.txt
uvicorn app.main:app --app-dir backend --reload
```

The local base URL is `http://127.0.0.1:8000`. FastAPI's default interactive
documentation and OpenAPI schema are available at `/docs`, `/redoc`, and
`/openapi.json`.

The SQLite database defaults to `backend/data/schema_registry.sqlite3`.
`VIGIA_DATABASE_PATH` can be set to choose another file. Authentication and
authorization are not implemented; do not expose administrative operations to
untrusted clients.

## Endpoint index

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | Return static service metadata |
| `GET` | `/health` | Return health status and a UTC timestamp |
| `POST` | `/datasets/analyze` | Upload and analyze a CSV dataset |
| `PUT` | `/datasets/{dataset_id}/schema` | Approve a new schema version |
| `GET` | `/datasets/{dataset_id}/schema/history` | Return registered schema versions |
| `GET` | `/incidents` | List incidents with filters and pagination |
| `GET` | `/incidents/{incident_id}` | Retrieve one incident |
| `GET` | `/incidents/{incident_id}/occurrences` | List an incident's occurrences |
| `GET` | `/incidents/{incident_id}/history` | List an incident's status history |
| `PATCH` | `/incidents/{incident_id}/status` | Change an incident's status |

## Service endpoints

### `GET /`

Returns static service metadata. This is not the health check.

```json
{
  "name": "VIGIA",
  "description": "Data observability platform",
  "version": "0.1.0",
  "status": "operational"
}
```

### `GET /health`

Returns the current health status and an ISO 8601 UTC timestamp.

```json
{
  "status": "healthy",
  "timestamp": "2026-01-02T03:04:00+00:00"
}
```

## Dataset analysis

### `POST /datasets/analyze`

Accepts `multipart/form-data` with these fields:

| Name | Location | Required | Description |
|---|---|---:|---|
| `file` | Form file | Yes | CSV upload |
| `dataset_id` | Form field | Yes | Stable identifier used to associate schema versions and incidents |
| `Idempotency-Key` | HTTP header | Yes | Analysis identifier used by incident processing |

Example:

```sh
curl -X POST "http://127.0.0.1:8000/datasets/analyze" \
  -H "Idempotency-Key: customers-analysis-001" \
  -F "dataset_id=customers" \
  -F "file=@customers.csv;type=text/csv"
```

#### Upload validation

- The file must have a filename ending in `.csv` (case-insensitive).
- The upload must contain data and parse into a DataFrame with data rows and
  columns.
- The parsed DataFrame is checked for duplicate column names and blank column
  names.
- The maximum upload size is 10 MiB (`10 * 1024 * 1024` bytes).
- `dataset_id` must be non-empty and have no leading or trailing whitespace.
- The filename is required.
- `Idempotency-Key` must not be empty or whitespace-only and must not have
  leading or trailing whitespace.

#### `Idempotency-Key` behavior

The header is required and its value is passed to the dataset analysis as
`analysis_id`. Incident processing uses that identifier to avoid recording a
duplicate occurrence for the same incident and analysis. Reusing the key does
not skip the analysis, cache or replay a prior response, or bind the key to a
request payload. A request using the same key can therefore run again; the
incident occurrence may be ignored as a duplicate.

#### Successful response

The response contains the dataset and baseline information, a quality report,
schema drift issues, and incident-processing counts. This representative
response shows a first analysis with no detected issues:

```json
{
  "dataset_id": "customers",
  "baseline_version": 1,
  "baseline_created": true,
  "quality_report": {
    "dataset_name": "customers.csv",
    "total_rows": 3,
    "total_columns": 2,
    "missing_cells": 0,
    "duplicate_rows": 0,
    "quality_score": 100.0,
    "columns": [
      {
        "name": "id",
        "data_type": "int64",
        "missing_count": 0,
        "missing_percentage": 0.0,
        "unique_count": 3
      },
      {
        "name": "name",
        "data_type": "str",
        "missing_count": 0,
        "missing_percentage": 0.0,
        "unique_count": 3
      }
    ],
    "issues": []
  },
  "schema_drift_issues": [],
  "incident_processing": {
    "created_count": 0,
    "updated_count": 0,
    "reopened_count": 0,
    "ignored_occurrence_count": 0
  }
}
```

The quality report's `issues` and the top-level `schema_drift_issues` contain
objects with `code`, `title`, `description`, `severity`, `column`,
`affected_rows`, and `examples`. `column` may be `null`; `examples` is an array
of integer row-index values. Quality issue codes currently include `MISSING_VALUES`,
`DUPLICATE_ROWS`, and `NUMERIC_ANOMALIES`. Schema drift codes are
`COLUMN_ADDED`, `COLUMN_REMOVED`, and `COLUMN_TYPE_CHANGED`.
`unique_count` is the number of distinct values, calculated using
`pandas.Series.nunique()`.

On the first analysis, the inferred schema is registered as the dataset's
baseline and no drift issues are returned. Later analyses compare against the
stored baseline. Detected drift does not automatically replace that baseline.

#### Analysis errors

| Status | Condition |
|---:|---|
| `400` | Invalid CSV, empty file/data, missing filename, or invalid `dataset_id` |
| `413` | Upload exceeds 10 MiB |
| `422` | Missing required request fields/header, or invalid `Idempotency-Key` |
| `500` | Unexpected analysis or persistence failure |

FastAPI validation errors use its standard validation response. For example,
an omitted required header or form field is rejected before analysis.

## Schema registry

### `PUT /datasets/{dataset_id}/schema`

Explicitly appends a schema version. The dataset must already have a registered
baseline. The request body must contain `schema` (a mapping of column names to
type strings) and `expected_version`, declared as an integer (`int`) with a
minimum value of 1. This field does not use strict validation (`strict=True` is
not set).

```json
{
  "schema": {
    "id": "int64",
    "name": "str",
    "email": "str"
  },
  "expected_version": 1
}
```

Extra request fields are rejected. Schema column names and type values must be
strings, and type strings must not be empty or whitespace-only. The
`expected_version` must match the current version; a successful approval
creates the next version.

Successful response:

```json
{
  "dataset_id": "customers",
  "version": 2,
  "schema": {
    "id": "int64",
    "name": "str",
    "email": "str"
  },
  "created_at": "2026-01-02T03:04:00+00:00"
}
```

| Status | Condition |
|---:|---|
| `404` | No schema is registered for the dataset |
| `409` | `expected_version` is stale |
| `422` | Invalid request body, schema, or version |
| `500` | Unexpected approval or persistence failure |

This endpoint has no authentication or authorization. The implementation
describes it as an administrative operation; protect it before exposing it to
untrusted clients.

### `GET /datasets/{dataset_id}/schema/history`

Returns schema versions in ascending version order.

```json
{
  "dataset_id": "customers",
  "versions": [
    {
      "version": 1,
      "schema": {
        "id": "int64",
        "name": "str"
      },
      "created_at": "2026-01-02T03:04:00+00:00"
    }
  ]
}
```

| Status | Condition |
|---:|---|
| `200` | One or more versions are registered |
| `400` | `dataset_id` is empty or has leading or trailing whitespace |
| `404` | No schema history exists for the dataset |
| `500` | Unexpected lookup or persistence failure |

## Incident management

Incident statuses are `OPEN`, `ACKNOWLEDGED`, and `RESOLVED`. Severities are
`low`, `medium`, `high`, and `critical`.

### Incident response

`GET /incidents/{incident_id}` and successful status changes return an incident
object with this shape:

The IDs, timestamps, and fingerprints in the examples below are illustrative.

```json
{
  "incident_id": "incident-001",
  "dataset_id": "customers",
  "fingerprint": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "code": "MISSING_VALUES",
  "title": "Missing values detected",
  "description": "Column 'email' contains missing values.",
  "column": "email",
  "severity": "high",
  "status": "OPEN",
  "created_at": "2026-01-02T03:04:00+00:00",
  "updated_at": "2026-01-02T03:04:00+00:00",
  "first_seen_at": "2026-01-02T03:04:00+00:00",
  "last_seen_at": "2026-01-02T03:04:00+00:00",
  "resolved_at": null,
  "occurrence_count": 1,
  "revision": 1
}
```

All timestamps are serialized as ISO 8601 strings. `column` and `resolved_at`
can be `null`.

### `GET /incidents`

Supported query filters:

| Parameter | Type | Description |
|---|---|---|
| `dataset_id` | string | Match a dataset identifier |
| `status` | `IncidentStatus` | `OPEN`, `ACKNOWLEDGED`, or `RESOLVED` |
| `severity` | `IncidentSeverity` | `low`, `medium`, `high`, or `critical` |
| `code` | string | Match an incident issue code |
| `limit` | integer | 1–100; defaults to 50 |
| `offset` | integer | Non-negative; defaults to 0 |

Whitespace-only `dataset_id` and `code`, unsupported enum values, and invalid
pagination values are rejected with `422`.

Response:

```json
{
  "items": [
    {
      "incident_id": "incident-001",
      "dataset_id": "customers",
      "fingerprint": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
      "code": "MISSING_VALUES",
      "title": "Missing values detected",
      "description": "Column 'email' contains missing values.",
      "column": "email",
      "severity": "high",
      "status": "OPEN",
      "created_at": "2026-01-02T03:04:00+00:00",
      "updated_at": "2026-01-02T03:04:00+00:00",
      "first_seen_at": "2026-01-02T03:04:00+00:00",
      "last_seen_at": "2026-01-02T03:04:00+00:00",
      "resolved_at": null,
      "occurrence_count": 1,
      "revision": 1
    }
  ],
  "limit": 50,
  "offset": 0
}
```

An empty result is returned as `200` with `"items": []`. The API does not
provide a total count or `total_pages`.

### `GET /incidents/{incident_id}`

Returns the incident response object. Returns `404` if the incident does not
exist. A blank incident ID is rejected with `422`.

### `GET /incidents/{incident_id}/occurrences`

Accepts `limit` (1–100, default 50) and `offset` (non-negative, default 0).
The parent incident must exist; otherwise the endpoint returns `404`. An
existing incident with no occurrences returns `200` and an empty `items` list.

```json
{
  "items": [
    {
      "occurrence_id": "occurrence-001",
      "incident_id": "incident-001",
      "analysis_id": "customers-analysis-001",
      "detected_at": "2026-01-02T03:04:00+00:00",
      "code": "MISSING_VALUES",
      "title": "Missing values detected",
      "description": "Column 'email' contains missing values.",
      "column": "email",
      "severity": "high",
      "affected_rows": 2,
      "examples": [1, 3]
    }
  ],
  "limit": 50,
  "offset": 0
}
```

### `GET /incidents/{incident_id}/history`

Accepts `limit` (1–100, default 50) and `offset` (non-negative, default 0).
The parent incident must exist; otherwise the endpoint returns `404`. An
existing incident with no status history returns `200` and an empty `items`
list.

```json
{
  "items": [
    {
      "change_id": "change-001",
      "incident_id": "incident-001",
      "from_status": "OPEN",
      "to_status": "ACKNOWLEDGED",
      "changed_at": "2026-01-02T03:04:00+00:00",
      "actor": "api",
      "reason": "Reviewed by the on-call operator."
    }
  ],
  "limit": 50,
  "offset": 0
}
```

For both collection endpoints, responses contain `items`, `limit`, and
`offset`; there is no total count. Invalid pagination and blank incident IDs
return `422`.

### `PATCH /incidents/{incident_id}/status`

Request body:

```json
{
  "status": "ACKNOWLEDGED",
  "expected_revision": 1,
  "reason": "Reviewed by the on-call operator."
}
```

All three fields are required:

- `status` must be a valid `IncidentStatus`.
- `expected_revision` must be a strict integer greater than or equal to 1.
  Booleans and numeric strings are not accepted as integers.
- `reason` must not be empty or whitespace-only. A nonblank reason is retained
  as submitted, including any surrounding whitespace.
- Extra fields are rejected. In particular, clients cannot supply `actor`.
  The API sets the history actor to `"api"` internally; this is not an
  authenticated identity.

Allowed manual transitions:

| Current status | Allowed target status |
|---|---|
| `OPEN` | `ACKNOWLEDGED`, `RESOLVED` |
| `ACKNOWLEDGED` | `OPEN`, `RESOLVED` |
| `RESOLVED` | No different status |

Repeating the current status succeeds as a no-op when the supplied revision is
current. It does not increment `revision` or create a history record. A
successful state change increments the revision and appends a status-history
record in the same transaction. Resolving an incident sets `resolved_at`;
valid transitions to a non-resolved state clear it.

The API does not permit manually reopening a resolved incident. Automatic
incident processing can reopen a resolved incident when the same issue is
detected again; this is separate from the PATCH operation.

| Status | Condition |
|---:|---|
| `200` | Status changed, or requested status was already current |
| `404` | Incident does not exist |
| `409` | Stale revision or invalid transition |
| `422` | Invalid path value or request body |
| `500` | Unexpected status-change or persistence failure |

A stale-revision conflict includes `expected_revision` and `current_revision`
in its `detail` object. An invalid-transition conflict returns a string in
`detail`.

## Shared response and error behavior

Incident list, occurrence, and status-history responses contain only the
requested page and do not include a total count. Schema history is not
paginated; it returns all registered versions in ascending order. Response DTOs
serialize enum values as strings, timestamps as ISO 8601 strings, and
occurrence `examples` as JSON arrays.

Unexpected database and persistence errors are reported as `500`, not
converted to client errors. FastAPI request validation errors use HTTP `422`;
their detailed `detail` structure can vary with the invalid field.

## Not implemented

- Authentication and authorization.
- A client-provided or authenticated actor for status changes.
- Full request idempotency, response replay, or payload binding for
  `Idempotency-Key`.
- Total-count pagination metadata.
- A frontend.
