import pandas as pd

from app.quality.schemas import QualityIssue


def detect_missing_values(df: pd.DataFrame) -> list[QualityIssue]:
    """Detect columns containing missing values."""
    issues = []

    for column in df.columns:
        missing_count = int(df[column].isna().sum())

        if missing_count == 0:
            continue

        examples = (
            df.index[df[column].isna()]
            .tolist()[:5]
        )

        percentage = (missing_count / len(df)) * 100 if len(df) else 0

        severity = (
            "critical" if percentage >= 50
            else "high" if percentage >= 20
            else "medium" if percentage >= 5
            else "low"
        )

        issues.append(
            QualityIssue(
                code="MISSING_VALUES",
                title="Missing values detected",
                description=(
                    f"Column '{column}' contains "
                    f"{missing_count} missing values "
                    f"({percentage:.2f}%)."
                ),
                severity=severity,
                column=str(column),
                affected_rows=missing_count,
                examples=[int(index) for index in examples],
            )
        )

    return issues


def detect_duplicate_rows(df: pd.DataFrame) -> list[QualityIssue]:
    """Detect duplicated rows in a dataset."""
    duplicate_mask = df.duplicated(keep="first")
    duplicate_count = int(duplicate_mask.sum())

    if duplicate_count == 0:
        return []

    examples = df.index[duplicate_mask].tolist()[:5]

    return [
        QualityIssue(
            code="DUPLICATE_ROWS",
            title="Duplicate rows detected",
            description=(
                f"The dataset contains {duplicate_count} "
                "duplicated rows."
            ),
            severity="high" if duplicate_count >= 10 else "medium",
            affected_rows=duplicate_count,
            examples=[int(index) for index in examples],
        )
    ]
