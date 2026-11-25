"""Application orchestration for dataset quality and schema drift analysis."""

from dataclasses import dataclass

import pandas as pd

from app.incidents.service import IncidentProcessingResult, IncidentService
from app.quality.engine import analyze_dataset
from app.quality.schemas import QualityIssue, QualityReport
from app.schema_drift.detector import detect_schema_drift
from app.schema_registry.service import SchemaRegistryService


@dataclass(frozen=True)
class DatasetAnalysisResult:
    """Quality analysis and schema-registry outcome for one dataset upload."""

    quality_report: QualityReport
    dataset_id: str
    baseline_version: int
    baseline_created: bool
    schema_drift_issues: list[QualityIssue]
    incident_processing: IncidentProcessingResult


class DatasetAnalysisService:
    """Analyze a dataset and compare it with its persisted schema baseline."""

    def __init__(
        self,
        schema_registry: SchemaRegistryService,
        incident_service: IncidentService,
    ) -> None:
        self._schema_registry = schema_registry
        self._incident_service = incident_service

    def analyze(
        self,
        df: pd.DataFrame,
        dataset_id: str,
        dataset_name: str,
        *,
        analysis_id: str,
    ) -> DatasetAnalysisResult:
        """Analyze quality, report drift, and persist the detected issues."""
        quality_report = analyze_dataset(df, dataset_name)
        current_schema = {
            column.name: column.data_type
            for column in quality_report.columns
        }

        baseline, baseline_created = self._schema_registry.get_or_create_baseline(
            dataset_id,
            current_schema,
        )

        schema_drift_issues = (
            []
            if baseline_created
            else detect_schema_drift(df, baseline.schema)
        )
        all_issues = [
            *quality_report.issues,
            *schema_drift_issues,
        ]
        incident_processing = self._incident_service.process_issues(
            dataset_id,
            analysis_id,
            all_issues,
        )

        return DatasetAnalysisResult(
            quality_report=quality_report,
            dataset_id=dataset_id,
            baseline_version=baseline.version,
            baseline_created=baseline_created,
            schema_drift_issues=schema_drift_issues,
            incident_processing=incident_processing,
        )
