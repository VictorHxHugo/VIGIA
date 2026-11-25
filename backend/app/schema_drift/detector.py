"""Detect structural differences between a DataFrame and an expected schema."""

import pandas as pd

from app.quality.schemas import QualityIssue


def detect_schema_drift(
    df: pd.DataFrame,
    expected_schema: dict[str, str],
) -> list[QualityIssue]:
    """Compare DataFrame columns and Pandas dtypes with an expected schema."""
    if any(not isinstance(column, str) for column in df.columns):
        raise ValueError("DataFrame column names must all be strings.")
    if df.columns.has_duplicates:
        raise ValueError(
            "DataFrame column names must be unique; duplicates are not allowed."
        )

    if any(not isinstance(column, str) for column in expected_schema):
        raise ValueError("Expected schema column names must all be strings.")
    for expected_dtype in expected_schema.values():
        if not isinstance(expected_dtype, str):
            raise ValueError("Expected schema data types must all be strings.")
        if not expected_dtype.strip():
            raise ValueError("Expected schema data types must be non-empty strings.")

    issues: list[QualityIssue] = []
    current_columns = set(df.columns)
    expected_columns = set(expected_schema)

    for column in df.columns:
        if column not in expected_columns:
            issues.append(
                QualityIssue(
                    code="COLUMN_ADDED",
                    title="Column added",
                    description=(
                        f"Column '{column}' is present in the DataFrame "
                        "but not in the expected schema."
                    ),
                    severity="medium",
                    column=column,
                    affected_rows=0,
                    examples=[],
                )
            )

    for column_name in expected_schema:
        if column_name not in current_columns:
            issues.append(
                QualityIssue(
                    code="COLUMN_REMOVED",
                    title="Column removed",
                    description=(
                        f"Column '{column_name}' is present in the expected "
                        "schema but missing from the DataFrame."
                    ),
                    severity="high",
                    column=column_name,
                    affected_rows=0,
                    examples=[],
                )
            )

    for column in df.columns:
        if column not in expected_columns:
            continue

        actual_dtype = str(df[column].dtype)
        expected_dtype = expected_schema[column]
        if actual_dtype != expected_dtype:
            issues.append(
                QualityIssue(
                    code="COLUMN_TYPE_CHANGED",
                    title="Column type changed",
                    description=(
                        f"Column '{column}' has Pandas dtype "
                        f"'{actual_dtype}', expected '{expected_dtype}'."
                    ),
                    severity="high",
                    column=column,
                    affected_rows=0,
                    examples=[],
                )
            )

    return issues
