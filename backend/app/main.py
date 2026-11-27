"""FastAPI application for VIGIA."""

import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

import anyio
from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.encoders import jsonable_encoder
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.auth.repository import AuthRepository
from app.auth.router import router as auth_router
from app.auth.dependencies import Permission, require_permission
from app.auth.models import Principal
from app.auth.service import AuthService
from app.auth.sqlite_repository import SQLiteAuthRepository
from app.core.analysis_limits import AnalysisDimensionExceededError
from app.datasets.repository import DatasetAnalysisRepository
from app.datasets.service import DatasetAnalysisResult, DatasetAnalysisService
from app.datasets.sqlite_repository import SQLiteDatasetAnalysisRepository
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
_DATASET_ANALYSIS_SEMAPHORE = threading.BoundedSemaphore(1)


def _resolve_auth_cookie_secure() -> bool:
    """Resolve cookie security, allowing insecure cookies only in development."""
    configured_value = os.environ.get("VIGIA_COOKIE_SECURE")
    if configured_value is None:
        return True

    normalized_value = configured_value.strip().casefold()
    if normalized_value in {"true", "1", "yes"}:
        return True
    if normalized_value not in {"false", "0", "no"}:
        raise ValueError("VIGIA_COOKIE_SECURE must be a boolean value.")

    environment = os.environ.get("VIGIA_ENV", "").strip().casefold()
    if environment != "development":
        raise ValueError(
            "VIGIA_COOKIE_SECURE may be disabled only when "
            "VIGIA_ENV=development."
        )
    return False


def _process_dataset_analysis(
    file_content: bytes,
    filename: str,
    dataset_id: str,
    analysis_id: str,
    analysis_service: DatasetAnalysisService,
) -> DatasetAnalysisResult:
    """Parse and analyze one upload synchronously in a worker thread."""
    df = read_csv_file(file_content, filename)
    return analysis_service.analyze(
        df,
        dataset_id=dataset_id,
        dataset_name=filename,
        analysis_id=analysis_id,
    )


class SchemaApprovalRequest(BaseModel):
    """Request body for explicitly approving a dataset schema."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_: dict[str, str] = Field(alias="schema")
    expected_version: int = Field(ge=1)


def create_app(
    repository: SchemaRegistryRepository | None = None,
    incident_repository: IncidentRepository | None = None,
    *,
    dataset_analysis_repository: DatasetAnalysisRepository | None = None,
    auth_repository: AuthRepository | None = None,
) -> FastAPI:
    """Create the API and wire its application services."""
    auth_cookie_secure = _resolve_auth_cookie_secure()
    uses_default_business_repositories = (
        repository is None and incident_repository is None
    )
    database_path: Path | None = None
    schema_registry_repository: SchemaRegistryRepository
    incident_persistence_repository: IncidentRepository
    if uses_default_business_repositories:
        if dataset_analysis_repository is not None:
            raise ValueError(
                "repository and incident_repository must be provided together "
                "when injecting dataset_analysis_repository."
            )

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
        dataset_analysis_repository = SQLiteDatasetAnalysisRepository(
            database_path,
            schema_registry_repository,
            incident_persistence_repository,
            busy_timeout_ms=schema_registry_repository.busy_timeout_ms,
        )
    elif repository is None or incident_repository is None:
        raise ValueError(
            "repository and incident_repository must be provided together."
        )
    else:
        schema_registry_repository = repository
        incident_persistence_repository = incident_repository
        if dataset_analysis_repository is None:
            if not isinstance(
                schema_registry_repository,
                SQLiteSchemaRegistryRepository,
            ) or not isinstance(
                incident_persistence_repository,
                SQLiteIncidentRepository,
            ):
                raise ValueError(
                    "dataset_analysis_repository is required when injected "
                    "repositories are not SQLite repositories."
                )
            dataset_analysis_repository = SQLiteDatasetAnalysisRepository(
                schema_registry_repository.database_path,
                schema_registry_repository,
                incident_persistence_repository,
                busy_timeout_ms=schema_registry_repository.busy_timeout_ms,
            )
        elif isinstance(
            dataset_analysis_repository,
            SQLiteDatasetAnalysisRepository,
        ):
            if not isinstance(
                schema_registry_repository,
                SQLiteSchemaRegistryRepository,
            ) or not isinstance(
                incident_persistence_repository,
                SQLiteIncidentRepository,
            ):
                raise ValueError(
                    "A SQLite dataset analysis repository requires SQLite "
                    "schema and incident repositories."
                )
            analysis_path = Path(dataset_analysis_repository.database_path).resolve()
            if (
                Path(schema_registry_repository.database_path).resolve()
                != analysis_path
                or Path(incident_persistence_repository.database_path).resolve()
                != analysis_path
            ):
                raise ValueError(
                    "Injected SQLite repositories must use the same database path."
                )
            if (
                schema_registry_repository.busy_timeout_ms
                != dataset_analysis_repository.busy_timeout_ms
                or incident_persistence_repository.busy_timeout_ms
                != dataset_analysis_repository.busy_timeout_ms
            ):
                raise ValueError(
                    "Injected SQLite repositories must use the same busy timeout."
                )

    if auth_repository is None:
        if not uses_default_business_repositories or database_path is None:
            raise ValueError(
                "auth_repository is required when injecting custom business "
                "repositories."
            )
        auth_persistence_repository: AuthRepository = SQLiteAuthRepository(
            database_path,
            busy_timeout_ms=5000,
        )
    else:
        auth_persistence_repository = auth_repository

    if isinstance(auth_persistence_repository, SQLiteAuthRepository):
        sqlite_business_repositories = (
            (
                schema_registry_repository,
                SQLiteSchemaRegistryRepository,
            ),
            (
                incident_persistence_repository,
                SQLiteIncidentRepository,
            ),
            (
                dataset_analysis_repository,
                SQLiteDatasetAnalysisRepository,
            ),
        )
        auth_database_path = Path(
            auth_persistence_repository.database_path
        ).resolve()
        for business_repository, repository_type in sqlite_business_repositories:
            if not isinstance(business_repository, repository_type):
                continue
            if (
                Path(business_repository.database_path).resolve()
                != auth_database_path
            ):
                raise ValueError(
                    "Injected SQLite repositories must use the same database path."
                )
            if (
                business_repository.busy_timeout_ms
                != auth_persistence_repository.busy_timeout_ms
            ):
                raise ValueError(
                    "Injected SQLite repositories must use the same busy timeout."
                )

    schema_registry_service = SchemaRegistryService(schema_registry_repository)
    incident_service = IncidentService(incident_persistence_repository)
    incident_management_service = IncidentManagementService(
        incident_persistence_repository
    )
    dataset_analysis_service = DatasetAnalysisService(
        schema_registry_service,
        incident_service,
        dataset_analysis_repository,
    )

    app = FastAPI(
        title="VIGIA API",
        description="Data observability and reliability platform.",
        version="0.1.0",
    )
    app.state.schema_registry_service = schema_registry_service
    app.state.dataset_analysis_service = dataset_analysis_service
    app.state.dataset_analysis_repository = dataset_analysis_repository
    app.state.incident_management_service = incident_management_service
    app.state.auth_repository = auth_persistence_repository
    app.state.auth_service = AuthService(auth_persistence_repository)
    app.state.auth_cookie_secure = auth_cookie_secure
    app.include_router(auth_router)

    @app.exception_handler(RequestValidationError)
    async def handle_request_validation_error(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        if request.url.path != "/auth/login":
            return await request_validation_exception_handler(request, exc)

        errors = [
            {key: value for key, value in error.items() if key != "input"}
            for error in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={"detail": jsonable_encoder(errors)},
        )

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
        principal: Annotated[
            Principal,
            Depends(require_permission(Permission.INCIDENT_READ)),
        ],
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
        principal: Annotated[
            Principal,
            Depends(require_permission(Permission.INCIDENT_READ)),
        ],
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
        principal: Annotated[
            Principal,
            Depends(require_permission(Permission.INCIDENT_READ)),
        ],
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
    def get_incident(
        incident_id: str,
        principal: Annotated[
            Principal,
            Depends(require_permission(Permission.INCIDENT_READ)),
        ],
    ):
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
        principal: Annotated[
            Principal,
            Depends(require_permission(Permission.INCIDENT_STATUS_CHANGE)),
        ],
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
                actor=str(principal.user_id),
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
        principal: Annotated[
            Principal,
            Depends(require_permission(Permission.DATASET_ANALYZE)),
        ],
    ):
        """Analyze a dataset; clients should reuse the key on communication retries."""
        semaphore_acquired = False
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

            if not _DATASET_ANALYSIS_SEMAPHORE.acquire(blocking=False):
                raise HTTPException(
                    status_code=503,
                    detail="A dataset analysis is already in progress. Retry later.",
                    headers={"Retry-After": "1"},
                )
            semaphore_acquired = True

            content = await file.read(MAX_UPLOAD_SIZE + 1)

            if len(content) > MAX_UPLOAD_SIZE:
                raise HTTPException(
                    status_code=413,
                    detail="File exceeds the 10 MB upload limit.",
                )

            result = await anyio.to_thread.run_sync(
                _process_dataset_analysis,
                content,
                file.filename,
                dataset_id,
                analysis_id,
                app.state.dataset_analysis_service,
                abandon_on_cancel=False,
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

        except AnalysisDimensionExceededError as exc:
            raise HTTPException(
                status_code=413,
                detail=str(exc),
            ) from exc

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
            try:
                await file.close()
            finally:
                if semaphore_acquired:
                    _DATASET_ANALYSIS_SEMAPHORE.release()

    @app.put("/datasets/{dataset_id}/schema")
    def approve_schema(
        dataset_id: str,
        request: SchemaApprovalRequest,
        principal: Annotated[
            Principal,
            Depends(require_permission(Permission.SCHEMA_APPROVE)),
        ],
    ):
        """Administrative operation; do not expose publicly before authentication."""
        try:
            approved = app.state.schema_registry_service.approve_schema(
                dataset_id,
                request.schema_,
                request.expected_version,
                approved_by_user_id=principal.user_id,
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
    def schema_history(
        dataset_id: str,
        principal: Annotated[
            Principal,
            Depends(require_permission(Permission.SCHEMA_READ)),
        ],
    ):
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
                    "approved_by_user_id": (
                        str(version.approved_by_user_id)
                        if version.approved_by_user_id is not None
                        else None
                    ),
                }
                for version in versions
            ],
        }

    return app


app = create_app()
