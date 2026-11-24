from typing import Literal

from pydantic import BaseModel, Field


Severity = Literal["low", "medium", "high", "critical"]


class QualityIssue(BaseModel):
    code: str
    title: str
    description: str
    severity: Severity
    column: str | None = None
    affected_rows: int = Field(ge=0)
    examples: list[int] = Field(default_factory=list)


class ColumnProfile(BaseModel):
    name: str
    data_type: str
    missing_count: int = Field(ge=0)
    missing_percentage: float = Field(ge=0, le=100)
    unique_count: int = Field(ge=0)


class QualityReport(BaseModel):
    dataset_name: str
    total_rows: int = Field(ge=0)
    total_columns: int = Field(ge=0)
    missing_cells: int = Field(ge=0)
    duplicate_rows: int = Field(ge=0)
    quality_score: float = Field(ge=0, le=100)
    columns: list[ColumnProfile]
    issues: list[QualityIssue]
