import pandas as pd


MAX_ANALYSIS_ROWS = 100_000
MAX_ANALYSIS_COLUMNS = 200
MAX_ANALYSIS_CELLS = 2_000_000


class AnalysisDimensionExceededError(ValueError):
    """Raised when a dataset exceeds a configured analysis dimension."""

    def __init__(
        self,
        dimension: str,
        limit: int,
        observed: int | None,
    ) -> None:
        self.dimension = dimension
        self.limit = limit
        self.observed = observed

        message = (
            f"Analysis exceeds the configured maximum of {limit} "
            f"{dimension}."
        )
        if observed is not None:
            message += f" Observed {observed}."

        super().__init__(message)


def max_rows_for_columns(column_count: int) -> int:
    """Return the row ceiling allowed by the row and cell limits."""
    if column_count <= 0:
        return MAX_ANALYSIS_ROWS

    return min(MAX_ANALYSIS_ROWS, MAX_ANALYSIS_CELLS // column_count)


def validate_dataframe_dimensions(df: pd.DataFrame) -> None:
    """Reject DataFrames exceeding the configured analysis dimensions."""
    row_count, column_count = df.shape

    if column_count > MAX_ANALYSIS_COLUMNS:
        raise AnalysisDimensionExceededError(
            "columns",
            MAX_ANALYSIS_COLUMNS,
            column_count,
        )

    if row_count > MAX_ANALYSIS_ROWS:
        raise AnalysisDimensionExceededError(
            "rows",
            MAX_ANALYSIS_ROWS,
            row_count,
        )

    cell_count = row_count * column_count
    if cell_count > MAX_ANALYSIS_CELLS:
        raise AnalysisDimensionExceededError(
            "cells",
            MAX_ANALYSIS_CELLS,
            cell_count,
        )
