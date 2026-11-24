from io import BytesIO

import pandas as pd


class CSVValidationError(Exception):
    """Raised when a CSV file cannot be processed."""


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

    try:
        df = pd.read_csv(BytesIO(file_content))
    except (pd.errors.ParserError, UnicodeDecodeError) as exc:
        raise CSVValidationError(
            "Could not read the CSV file. Check its encoding and structure."
        ) from exc
    except Exception as exc:
        raise CSVValidationError(
            "An unexpected error occurred while reading the CSV file."
        ) from exc

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
