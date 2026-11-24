import pandas as pd

from app.anomaly.detector import detect_numeric_anomalies
from app.quality.rules import (
    detect_duplicate_rows,
    detect_missing_values,
)
from app.quality.schemas import ColumnProfile, QualityReport


def analyze_dataset(
    df: pd.DataFrame,
    dataset_name: str,
) -> QualityReport:
    """Analyze a dataset and generate a quality report."""

    total_rows, total_columns = df.shape
    total_cells = total_rows * total_columns

    missing_cells = int(df.isna().sum().sum())
    duplicate_rows = int(df.duplicated().sum())

    columns = []

    for column in df.columns:
        missing_count = int(df[column].isna().sum())

        missing_percentage = (
            (missing_count / total_rows) * 100
            if total_rows > 0
            else 0.0
        )

        columns.append(
            ColumnProfile(
                name=str(column),
                data_type=str(df[column].dtype),
                missing_count=missing_count,
                missing_percentage=round(missing_percentage, 2),
                unique_count=int(df[column].nunique()),
            )
        )

    issues = (
        detect_missing_values(df)
        + detect_duplicate_rows(df)
        + detect_numeric_anomalies(df)
    )

    missing_penalty = (
        (missing_cells / total_cells) * 70
        if total_cells > 0
        else 0
    )

    duplicate_penalty = (
        (duplicate_rows / total_rows) * 30
        if total_rows > 0
        else 0
    )

    quality_score = round(
        max(0.0, 100.0 - missing_penalty - duplicate_penalty),
        2,
    )

    return QualityReport(
        dataset_name=dataset_name,
        total_rows=total_rows,
        total_columns=total_columns,
        missing_cells=missing_cells,
        duplicate_rows=duplicate_rows,
        quality_score=quality_score,
        columns=columns,
        issues=issues,
    )
