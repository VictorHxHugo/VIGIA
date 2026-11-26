import numpy as np
import pandas as pd
import pytest

from app.core import analysis_limits
from app.core.analysis_limits import (
    AnalysisDimensionExceededError,
    max_rows_for_columns,
    validate_dataframe_dimensions,
)
from app.ingestion import csv_reader
from app.ingestion.csv_reader import (
    CSVValidationError,
    read_csv_file,
)


def _csv_with_dimensions(row_count: int, column_count: int) -> bytes:
    header = ",".join(f"column_{index}" for index in range(column_count))
    row = ",".join("1" for _ in range(column_count))
    return (header + "\n" + (row + "\n") * row_count).encode()


def test_reads_valid_csv():
    content = b"id,name\n1,Ana\n2,Bruno"

    df = read_csv_file(content, "customers.csv")

    assert len(df) == 2
    assert list(df.columns) == ["id", "name"]


def test_rejects_non_csv_file():
    with pytest.raises(CSVValidationError):
        read_csv_file(b"some content", "customers.xlsx")


def test_rejects_empty_file():
    with pytest.raises(CSVValidationError):
        read_csv_file(b"", "customers.csv")


def test_rejects_csv_without_data_rows():
    content = b"id,name\n"

    with pytest.raises(CSVValidationError):
        read_csv_file(content, "customers.csv")


def test_accepts_exactly_200_columns():
    df = read_csv_file(_csv_with_dimensions(1, 200), "wide.csv")

    assert df.shape == (1, 200)


def test_rejects_201_columns():
    with pytest.raises(AnalysisDimensionExceededError) as exc_info:
        read_csv_file(_csv_with_dimensions(1, 201), "too_wide.csv")

    assert exc_info.value.dimension == "columns"
    assert exc_info.value.limit == 200
    assert exc_info.value.observed == 201
    assert "columns" in str(exc_info.value)


def test_accepts_exactly_100000_rows():
    df = read_csv_file(_csv_with_dimensions(100_000, 1), "large.csv")

    assert df.shape == (100_000, 1)


def test_rejects_100001_rows():
    with pytest.raises(AnalysisDimensionExceededError) as exc_info:
        read_csv_file(_csv_with_dimensions(100_001, 1), "too_many_rows.csv")

    assert exc_info.value.dimension == "rows"
    assert exc_info.value.limit == 100_000
    assert exc_info.value.observed == 100_001


def test_max_rows_for_columns_respects_row_and_cell_limits():
    assert max_rows_for_columns(1) == 100_000
    assert max_rows_for_columns(20) == 100_000
    assert max_rows_for_columns(200) == 10_000
    assert max_rows_for_columns(0) == 100_000


def test_accepts_exactly_2000000_cells():
    df = pd.DataFrame(np.zeros((10_000, 200), dtype=np.uint8))

    validate_dataframe_dimensions(df)


def test_rejects_2000001_cells(monkeypatch):
    df = pd.DataFrame(np.zeros((666_667, 3), dtype=np.uint8))
    monkeypatch.setattr(analysis_limits, "MAX_ANALYSIS_ROWS", 666_667)

    with pytest.raises(AnalysisDimensionExceededError) as exc_info:
        validate_dataframe_dimensions(df)

    assert exc_info.value.dimension == "cells"
    assert exc_info.value.limit == 2_000_000
    assert exc_info.value.observed == 2_000_001


def test_shared_validation_accepts_exact_column_and_row_limits():
    validate_dataframe_dimensions(
        pd.DataFrame(index=range(100_000), columns=["value"])
    )
    validate_dataframe_dimensions(pd.DataFrame(columns=range(200)))


@pytest.mark.parametrize(
    ("row_count", "column_count", "dimension", "limit", "observed"),
    [
        (0, 201, "columns", 200, 201),
        (100_001, 1, "rows", 100_000, 100_001),
    ],
)
def test_shared_validation_rejects_excessive_dataframe_dimensions(
    row_count: int,
    column_count: int,
    dimension: str,
    limit: int,
    observed: int,
):
    df = pd.DataFrame(index=range(row_count), columns=range(column_count))

    with pytest.raises(AnalysisDimensionExceededError) as exc_info:
        validate_dataframe_dimensions(df)

    assert exc_info.value.dimension == dimension
    assert exc_info.value.limit == limit
    assert exc_info.value.observed == observed


def test_shared_validation_handles_empty_dataframe_without_division_by_zero():
    validate_dataframe_dimensions(pd.DataFrame())
    assert max_rows_for_columns(0) == analysis_limits.MAX_ANALYSIS_ROWS


def test_reads_delimiters_inside_quoted_fields():
    df = read_csv_file(
        b"id,description\n1,\"first, value\"\n2,\"second, value\"",
        "quoted.csv",
    )

    assert list(df.columns) == ["id", "description"]
    assert df["description"].tolist() == ["first, value", "second, value"]


def test_rejects_invalid_header():
    with pytest.raises(CSVValidationError):
        read_csv_file(b'"unterminated\n1\n', "invalid_header.csv")


def test_reads_only_one_row_beyond_effective_limit(monkeypatch):
    monkeypatch.setattr(analysis_limits, "MAX_ANALYSIS_ROWS", 2)
    monkeypatch.setattr(analysis_limits, "MAX_ANALYSIS_CELLS", 4)
    original_read_csv = pd.read_csv
    nrows_values = []

    def recording_read_csv(content, **kwargs):
        nrows_values.append(kwargs["nrows"])
        return original_read_csv(content, **kwargs)

    monkeypatch.setattr(csv_reader.pd, "read_csv", recording_read_csv)

    with pytest.raises(AnalysisDimensionExceededError):
        read_csv_file(
            b"a,b\n1,2\n3,4\n5,6\n7,8\n",
            "too_many_rows.csv",
        )

    assert nrows_values == [0, 3]
