"""FastAPI application for VIGIA."""

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, Form, Header, HTTPException, Query, UploadFile
from pydantic import BaseModel, ConfigDict, Field

from app.datasets.service import DatasetAnalysisService
from app.incidents.api_schemas import (
    IncidentCollectionResponse,
    IncidentOccurrenceCollectionResponse,
    IncidentResponse,
    IncidentStatusChangeRequest,
    IncidentStatusHistoryCollectionResponse,
)
from app.incidents.management_service import IncidentManagementService
from app.incidents.models import (
    IncidentSeverity,
    IncidentStatus,
    InvalidIncidentTransitionError,
)
from app.incidents.repository import (
    IncidentFilters,
    IncidentNotFoundError,
    IncidentRepository,
    IncidentRevisionConflictError,
)
from app.incidents.service import IncidentService
from app.incidents.sqlite_repository import SQLiteIncidentRepository
from app.ingestion.csv_reader import (
    CSVValidationError,
    read_csv_file,
)
from app.schema_registry.repository import (
    DatasetNotFoundError,
    SchemaRegistryRepository,
    SchemaVersionConflictError,
)
from app.schema_registry.service import SchemaRegistryService
from app.schema_registry.sqlite_repository import SQLiteSchemaRegistryRepository


logger = logging.getLogger(__name__)

MAX_UPLOAD_SIZE = 10 * 1024 * 1024  # 10 MB


class SchemaApprovalRequest(BaseModel):
    """Request body for explicitly approving a dataset schema."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_: dict[str, str] = Field(alias="schema")
    expected_version: int = Field(ge=1)


def create_app(
    repository: SchemaRegistryRepository | None = None,
    incident_repository: IncidentRepository | None = None,
) -> FastAPI:
    """Create the API and wire its application services."""
    schema_registry_repository: SchemaRegistryRepository
    incident_persistence_repository: IncidentRepository
    if repository is None and incident_repository is None:
        configured_path = os.environ.get("VIGIA_DATABASE_PATH")
        if configured_path is not None and not configured_path.strip():
            raise ValueError("VIGIA_DATABASE_PATH must not be empty.")

        database_path = (
            Path(configured_path).expanduser()
            if configured_path is not None
            else Path(__file__).resolve().parent.parent
            / "data"
            / "schema_registry.sqlite3"
        )
        schema_registry_repository = SQLiteSchemaRegistryRepository(database_path)
        incident_persistence_repository = SQLiteIncidentRepository(database_path)
    elif repository is None or incident_repository is None:
        raise ValueError(
            "repository and incident_repository must be provided together."
        )
    else:
        schema_registry_repository = repository
        incident_persistence_repository = incident_repository

    schema_registry_service = SchemaRegistryService(schema_registry_repository)
    incident_service = IncidentService(incident_persistence_repository)
    incident_management_service = IncidentManagementService(
        incident_persistence_repository
    )
    dataset_analysis_service = DatasetAnalysisService(
        schema_registry_service,
        incident_service,
    )

    app = FastAPI(
        title="VIGIA API",
        description="Data observability and reliability platform.",
        version="0.1.0",
    )
    app.state.schema_registry_service = schema_registry_service
    app.state.dataset_analysis_service = dataset_analysis_service
    app.state.incident_management_service = incident_management_service

    @app.get("/")
    def root():
        return {
            "name": "VIGIA",
            "description": "Data observability platform",
            "version": "0.1.0",
            "status": "operational",
        }

    @app.get("/health")
    def health_check():
        return {
            "status": "healthy",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    @app.get("/incidents", response_model=IncidentCollectionResponse)
    def list_incidents(
        dataset_id: str | None = None,
        status: IncidentStatus | None = None,
        severity: IncidentSeverity | None = None,
        code: str | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ):
        try:
            filters = IncidentFilters(
                dataset_id=dataset_id,
                status=status,
                severity=severity,
                code=code,
                limit=limit,
                offset=offset,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        try:
            incidents = app.state.incident_management_service.list_incidents(
                filters
            )
        except Exception as exc:
            logger.exception("Incident listing failed.")
            raise HTTPException(
                status_code=500,
                detail="An unexpected error occurred while listing incidents.",
            ) from exc

        return {
            "items": incidents,
            "limit": limit,
            "offset": offset,
        }

    @app.get(
        "/incidents/{incident_id}/occurrences",
        response_model=IncidentOccurrenceCollectionResponse,
    )
    def list_incident_occurrences(
        incident_id: str,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ):
        if not incident_id.strip():
            raise HTTPException(
                status_code=422,
                detail="incident_id must be a non-empty string.",
            )

        try:
            occurrences = app.state.incident_management_service.list_occurrences(
                incident_id,
                limit=limit,
                offset=offset,
            )
        except IncidentNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("Incident occurrence lookup failed.")
            raise HTTPException(
                status_code=500,
                detail=(
                    "An unexpected error occurred while retrieving incident "
                    "occurrences."
                ),
            ) from exc

        return {
            "items": occurrences,
            "limit": limit,
            "offset": offset,
        }

    @app.get(
        "/incidents/{incident_id}/history",
        response_model=IncidentStatusHistoryCollectionResponse,
    )
    def list_incident_history(
        incident_id: str,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ):
        if not incident_id.strip():
            raise HTTPException(
                status_code=422,
                detail="incident_id must be a non-empty string.",
            )

        try:
            history = app.state.incident_management_service.list_status_history(
                incident_id,
                limit=limit,
                offset=offset,
            )
        except IncidentNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("Incident history lookup failed.")
            raise HTTPException(
                status_code=500,
                detail=(
                    "An unexpected error occurred while retrieving incident "
                    "history."
                ),
            ) from exc

        return {
            "items": history,
            "limit": limit,
            "offset": offset,
        }

    @app.get("/incidents/{incident_id}", response_model=IncidentResponse)
    def get_incident(incident_id: str):
        if not incident_id.strip():
            raise HTTPException(
                status_code=422,
                detail="incident_id must be a non-empty string.",
            )

        try:
            return app.state.incident_management_service.get_incident(incident_id)
        except IncidentNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("Incident lookup failed.")
            raise HTTPException(
                status_code=500,
                detail="An unexpected error occurred while retrieving the incident.",
            ) from exc

    @app.patch(
        "/incidents/{incident_id}/status",
        response_model=IncidentResponse,
    )
    def change_incident_status(
        incident_id: str,
        request: IncidentStatusChangeRequest,
    ):
        if not incident_id.strip():
            raise HTTPException(
                status_code=422,
                detail="incident_id must be a non-empty string.",
            )

        try:
            return app.state.incident_management_service.change_status(
                incident_id,
                request.status,
                expected_revision=request.expected_revision,
                actor="api",
                reason=request.reason,
            )
        except IncidentNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except IncidentRevisionConflictError as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": str(exc),
                    "expected_revision": exc.expected_revision,
                    "current_revision": exc.current_revision,
                },
            ) from exc
        except InvalidIncidentTransitionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("Incident status change failed.")
            raise HTTPException(
                status_code=500,
                detail="An unexpected error occurred while changing incident status.",
            ) from exc

    @app.post("/datasets/analyze")
    async def analyze_csv(
        file: Annotated[UploadFile, File(...)],
        dataset_id: Annotated[str, Form(...)],
        analysis_id: Annotated[
            str,
            Header(
                alias="Idempotency-Key",
                description=(
                    "Reuse the same key when retrying the same request after "
                    "a timeout or communication failure."
                ),
            ),
        ],
    ):
        """Analyze a dataset; clients should reuse the key on communication retries."""
        try:
            if (
                not analysis_id
                or analysis_id.isspace()
                or analysis_id[0].isspace()
                or analysis_id[-1].isspace()
            ):
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "Idempotency-Key must be non-empty and have no "
                        "surrounding whitespace."
                    ),
                )

            if not dataset_id or dataset_id != dataset_id.strip():
                raise HTTPException(
                    status_code=400,
                    detail="dataset_id must be non-empty and have no surrounding whitespace.",
                )

            if not file.filename:
                raise HTTPException(
                    status_code=400,
                    detail="Filename is required.",
                )

            content = await file.read(MAX_UPLOAD_SIZE + 1)

            if len(content) > MAX_UPLOAD_SIZE:
                raise HTTPException(
                    status_code=413,
                    detail="File exceeds the 10 MB upload limit.",
                )

            df = read_csv_file(content, file.filename)
            result = app.state.dataset_analysis_service.analyze(
                df,
                dataset_id=dataset_id,
                dataset_name=file.filename,
                analysis_id=analysis_id,
            )

            return {
                "dataset_id": result.dataset_id,
                "baseline_version": result.baseline_version,
                "baseline_created": result.baseline_created,
                "quality_report": result.quality_report.model_dump(mode="json"),
                "schema_drift_issues": [
                    issue.model_dump(mode="json")
                    for issue in result.schema_drift_issues
                ],
                "incident_processing": {
                    "created_count": result.incident_processing.created_count,
                    "updated_count": result.incident_processing.updated_count,
                    "reopened_count": result.incident_processing.reopened_count,
                    "ignored_occurrence_count": (
                        result.incident_processing.ignored_occurrence_count
                    ),
                },
            }

        except CSVValidationError as exc:
            raise HTTPException(
                status_code=400,
                detail=str(exc),
            ) from exc

        except HTTPException:
            raise

        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail=str(exc),
            ) from exc

        except Exception as exc:
            logger.exception("Dataset analysis failed.")
            raise HTTPException(
                status_code=500,
                detail="An unexpected error occurred during dataset analysis.",
            ) from exc

        finally:
            await file.close()

    @app.put("/datasets/{dataset_id}/schema")
    def approve_schema(
        dataset_id: str,
        request: SchemaApprovalRequest,
    ):
        """Administrative operation; do not expose publicly before authentication."""
        try:
            approved = app.state.schema_registry_service.approve_schema(
                dataset_id,
                request.schema_,
                request.expected_version,
            )
        except DatasetNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except SchemaVersionConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("Schema approval failed.")
            raise HTTPException(
                status_code=500,
                detail="An unexpected error occurred while approving the schema.",
            ) from exc

        return {
            "dataset_id": approved.dataset_id,
            "version": approved.version,
            "schema": approved.schema,
            "created_at": approved.created_at,
        }

    @app.get("/datasets/{dataset_id}/schema/history")
    def schema_history(dataset_id: str):
        try:
            versions = app.state.schema_registry_service.list_versions(dataset_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("Schema history lookup failed.")
            raise HTTPException(
                status_code=500,
                detail="An unexpected error occurred while retrieving schema history.",
            ) from exc

        if not versions:
            raise HTTPException(
                status_code=404,
                detail=f"No schema history found for dataset '{dataset_id}'.",
            )

        return {
            "dataset_id": dataset_id,
            "versions": [
                {
                    "version": version.version,
                    "schema": version.schema,
                    "created_at": version.created_at,
                }
                for version in versions
            ],
        }

    return app


app = create_app()
