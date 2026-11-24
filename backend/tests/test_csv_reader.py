import pytest

from app.ingestion.csv_reader import (
    CSVValidationError,
    read_csv_file,
)


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
