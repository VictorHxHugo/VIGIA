from io import BytesIO

import pandas as pd

from app.core.analysis_limits import (
    AnalysisDimensionExceededError,
    MAX_ANALYSIS_COLUMNS,
    max_rows_for_columns,
    validate_dataframe_dimensions,
)


class CSVValidationError(Exception):
    """Raised when a CSV file cannot be processed."""


def _read_csv_content(
    file_content: bytes,
    *,
    nrows: int,
) -> pd.DataFrame:
    try:
        return pd.read_csv(BytesIO(file_content), nrows=nrows)
    except (pd.errors.ParserError, UnicodeDecodeError) as exc:
        raise CSVValidationError(
            "Could not read the CSV file. Check its encoding and structure."
        ) from exc
    except Exception as exc:
        raise CSVValidationError(
            "An unexpected error occurred while reading the CSV file."
        ) from exc


def read_csv_file(
    file_content: bytes,
    filename: str,
) -> pd.DataFrame:
    """Read and validate a CSV file."""

    if not filename.lower().endswith(".csv"):
        raise CSVValidationError(
            "Invalid file format. Only CSV files are accepted."
        )

    if not file_content:
        raise CSVValidationError("The uploaded file is empty.")

    header = _read_csv_content(file_content, nrows=0)

    if len(header.columns) == 0:
        raise CSVValidationError(
            "The CSV file does not contain columns."
        )

    column_count = len(header.columns)
    if column_count > MAX_ANALYSIS_COLUMNS:
        raise AnalysisDimensionExceededError(
            "columns",
            MAX_ANALYSIS_COLUMNS,
            column_count,
        )

    effective_row_limit = max_rows_for_columns(column_count)
    df = _read_csv_content(file_content, nrows=effective_row_limit + 1)

    validate_dataframe_dimensions(df)

    if df.empty:
        raise CSVValidationError(
            "The CSV file does not contain any data rows."
        )

    if len(df.columns) == 0:
        raise CSVValidationError(
            "The CSV file does not contain columns."
        )

    if df.columns.duplicated().any():
        raise CSVValidationError(
            "The CSV file contains duplicated column names."
        )

    if any(str(column).strip() == "" for column in df.columns):
        raise CSVValidationError(
            "The CSV file contains empty column names."
        )

    return df
