# VIGIA — Architecture

## 1. Overview

VIGIA is a data observability platform designed to monitor datasets, identify data quality issues, detect anomalies and track incidents.

Its goal is to make data problems visible, explainable and traceable.

## 2. Technology Stack

- **Backend:** Python + FastAPI
- **Data Processing:** Pandas
- **Database:** PostgreSQL
- **Frontend:** React + TypeScript
- **Infrastructure:** Docker
- **Testing:** Pytest
- **CI/CD:** GitHub Actions

## 3. Backend Architecture

The backend follows a modular architecture:

- `core/`: application configuration and shared components.
- `ingestion/`: dataset ingestion and reading.
- `quality/`: data quality rules and validations.
- `anomaly/`: statistical anomaly detection.
- `incidents/`: incident creation, tracking and management.
- `tests/`: automated tests.

## 4. Core Capabilities

1. Dataset ingestion.
2. Data quality validation.
3. Schema drift detection.
4. Statistical anomaly detection.
5. Stale dataset identification.
6. Incident generation and tracking.
7. Explainable monitoring results.

## 5. Engineering Principles

- Deterministic detection logic.
- Modular and maintainable code.
- Automated testing.
- Clear error reporting.
- Reproducible results.
- No mandatory paid AI services.

## 6. Development Roadmap

1. Establish backend foundation.
2. Implement dataset ingestion.
3. Build data quality validation.
4. Implement anomaly detection.
5. Create incident management.
6. Expose functionality through REST APIs.
7. Build the frontend dashboard.
8. Add database persistence and Docker.
9. Implement automated CI checks.
