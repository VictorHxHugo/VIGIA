import pandas as pd

from app.quality.schemas import QualityIssue


def detect_numeric_anomalies(df: pd.DataFrame) -> list[QualityIssue]:
    """Detect numeric values outside the IQR-based lower and upper bounds."""
    issues: list[QualityIssue] = []

    if df.empty:
        return issues

    for column in df.select_dtypes(include="number").columns:
        valid_values = df[column].dropna()

        if len(valid_values) < 4:
            continue

        q1 = valid_values.quantile(0.25)
        q3 = valid_values.quantile(0.75)
        iqr = q3 - q1

        if pd.isna(iqr) or iqr == 0:
            continue

        lower_bound = q1 - 1.5 * iqr
        upper_bound = q3 + 1.5 * iqr
        anomaly_mask = (df[column] < lower_bound) | (df[column] > upper_bound)
        anomaly_mask &= df[column].notna()

        affected_rows = int(anomaly_mask.sum())
        if affected_rows == 0:
            continue

        anomaly_positions = anomaly_mask.to_numpy().nonzero()[0]
        examples = anomaly_positions[:5].tolist()
        percentage = (affected_rows / len(valid_values)) * 100

        severity = (
            "high"
            if affected_rows >= 5 or percentage >= 20
            else "medium"
        )

        issues.append(
            QualityIssue(
                code="NUMERIC_ANOMALIES",
                title="Numeric anomalies detected",
                description=(
                    f"Column '{column}' contains {affected_rows} values "
                    f"outside the IQR bounds "
                    f"[{lower_bound:.2f}, {upper_bound:.2f}]."
                ),
                severity=severity,
                column=str(column),
                affected_rows=affected_rows,
                examples=[int(index) for index in examples],
            )
        )

    return issues
