"""FastAPI application for VIGIA."""

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, ConfigDict, Field

from app.datasets.service import DatasetAnalysisService
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
) -> FastAPI:
    """Create the API and wire its application services."""
    if repository is None:
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
        repository = SQLiteSchemaRegistryRepository(database_path)

    schema_registry_service = SchemaRegistryService(repository)
    dataset_analysis_service = DatasetAnalysisService(schema_registry_service)

    app = FastAPI(
        title="VIGIA API",
        description="Data observability and reliability platform.",
        version="0.1.0",
    )
    app.state.schema_registry_service = schema_registry_service
    app.state.dataset_analysis_service = dataset_analysis_service

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

    @app.post("/datasets/analyze")
    async def analyze_csv(
        file: Annotated[UploadFile, File(...)],
        dataset_id: Annotated[str, Form(...)],
    ):
        try:
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
