import pandas as pd
import pytest

from app.schema_drift.detector import detect_schema_drift


def test_returns_no_issues_when_schema_matches():
    df = pd.DataFrame(
        {
            "id": pd.Series([1, 2], dtype="int64"),
            "name": pd.Series(["Ana", "Bruno"], dtype="object"),
        }
    )

    assert detect_schema_drift(
        df,
        {"id": "int64", "name": "object"},
    ) == []


def test_detects_added_column_as_medium_severity():
    df = pd.DataFrame(
        {
            "id": [1],
            "email": ["ana@example.com"],
        }
    )

    issues = detect_schema_drift(df, {"id": "int64"})

    assert len(issues) == 1
    assert issues[0].code == "COLUMN_ADDED"
    assert issues[0].column == "email"
    assert issues[0].severity == "medium"
    assert issues[0].affected_rows == 0
    assert issues[0].examples == []


def test_detects_removed_column_as_high_severity():
    df = pd.DataFrame({"id": [1]})

    issues = detect_schema_drift(
        df,
        {"id": "int64", "name": "object"},
    )

    assert len(issues) == 1
    assert issues[0].code == "COLUMN_REMOVED"
    assert issues[0].column == "name"
    assert issues[0].severity == "high"
    assert issues[0].affected_rows == 0
    assert issues[0].examples == []


def test_detects_changed_pandas_dtype_as_high_severity():
    df = pd.DataFrame({"id": pd.Series([1, 2], dtype="int64")})

    issues = detect_schema_drift(df, {"id": "object"})

    assert len(issues) == 1
    assert issues[0].code == "COLUMN_TYPE_CHANGED"
    assert issues[0].column == "id"
    assert issues[0].severity == "high"
    assert "int64" in issues[0].description
    assert "object" in issues[0].description
    assert issues[0].affected_rows == 0
    assert issues[0].examples == []


def test_detects_added_removed_and_type_changed_columns_together():
    df = pd.DataFrame(
        {
            "id": pd.Series([1], dtype="int64"),
            "email": pd.Series(["ana@example.com"], dtype="object"),
        }
    )

    issues = detect_schema_drift(
        df,
        {"id": "object", "name": "object"},
    )

    assert {issue.code for issue in issues} == {
        "COLUMN_ADDED",
        "COLUMN_REMOVED",
        "COLUMN_TYPE_CHANGED",
    }


def test_empty_dataframe_reports_expected_columns_as_removed():
    issues = detect_schema_drift(
        pd.DataFrame(),
        {"id": "int64"},
    )

    assert len(issues) == 1
    assert issues[0].code == "COLUMN_REMOVED"
    assert issues[0].column == "id"
    assert issues[0].affected_rows == 0
    assert issues[0].examples == []


def test_empty_dataframe_with_empty_schema_has_no_drift():
    assert detect_schema_drift(pd.DataFrame(), {}) == []


def test_empty_rows_preserve_and_compare_declared_dataframe_dtypes():
    df = pd.DataFrame({"id": pd.Series(dtype="int64")})

    assert detect_schema_drift(df, {"id": "int64"}) == []


def test_empty_schema_reports_each_dataframe_column_as_added():
    df = pd.DataFrame(
        {
            "id": pd.Series([1], dtype="int64"),
            "name": pd.Series(["Ana"], dtype="object"),
        }
    )

    issues = detect_schema_drift(df, {})

    assert [issue.code for issue in issues] == [
        "COLUMN_ADDED",
        "COLUMN_ADDED",
    ]
    assert [issue.column for issue in issues] == ["id", "name"]
    assert all(issue.severity == "medium" for issue in issues)


def test_rejects_dataframe_column_names_that_are_not_strings():
    df = pd.DataFrame([[1]], columns=[1])

    with pytest.raises(
        ValueError,
        match="DataFrame column names must all be strings",
    ):
        detect_schema_drift(df, {})


def test_rejects_duplicate_dataframe_column_names():
    df = pd.DataFrame([[1, 2]], columns=["id", "id"])

    with pytest.raises(ValueError, match="must be unique"):
        detect_schema_drift(df, {})


def test_rejects_non_string_expected_schema_column_names():
    df = pd.DataFrame()

    with pytest.raises(
        ValueError,
        match="Expected schema column names must all be strings",
    ):
        detect_schema_drift(df, {1: "int64"})


def test_rejects_empty_expected_dtype():
    df = pd.DataFrame()

    with pytest.raises(ValueError, match="must be non-empty strings"):
        detect_schema_drift(df, {"id": ""})


def test_rejects_non_string_expected_dtype():
    df = pd.DataFrame()

    with pytest.raises(
        ValueError,
        match="Expected schema data types must all be strings",
    ):
        detect_schema_drift(df, {"id": 1})
