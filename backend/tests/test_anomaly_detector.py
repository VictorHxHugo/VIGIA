import pandas as pd

from app.anomaly.detector import detect_numeric_anomalies


def test_detects_numeric_anomalies():
    df = pd.DataFrame(
        {
            "value": [10, 11, 12, 11, 13, 100],
        },
        index=[10, 11, 12, 13, 14, 15],
    )

    issues = detect_numeric_anomalies(df)

    assert len(issues) == 1
    assert issues[0].code == "NUMERIC_ANOMALIES"
    assert issues[0].column == "value"
    assert issues[0].affected_rows == 1
    assert issues[0].examples == [5]
    assert issues[0].severity == "medium"


def test_uses_positions_with_textual_indices():
    df = pd.DataFrame(
        {"value": [10, 11, 12, 11, 13, 100]},
        index=["a", "b", "c", "d", "e", "f"],
    )

    issues = detect_numeric_anomalies(df)

    assert len(issues) == 1
    assert issues[0].examples == [5]


def test_classifies_many_anomalies_as_high():
    df = pd.DataFrame(
        {"value": [10, 11, 12, 13, 14] * 3 + [100, 101, 102, 103, 104]}
    )

    issues = detect_numeric_anomalies(df)

    assert len(issues) == 1
    assert issues[0].severity == "high"


def test_returns_no_issues_for_normal_numeric_data():
    df = pd.DataFrame({"value": [10, 11, 12, 11, 13, 12]})

    assert detect_numeric_anomalies(df) == []


def test_ignores_textual_columns():
    df = pd.DataFrame({"name": ["Ana", "Bruno", "Carlos", "Daniel"]})

    assert detect_numeric_anomalies(df) == []


def test_missing_values_are_ignored():
    df = pd.DataFrame({"value": [10, 11, None, 12, 11, 13, 100]})

    issues = detect_numeric_anomalies(df)

    assert len(issues) == 1
    assert issues[0].affected_rows == 1
    assert issues[0].examples == [6]


def test_ignores_empty_columns_with_few_values_and_zero_iqr():
    df = pd.DataFrame(
        {
            "few_values": [1, 2, None, None, None],
            "zero_iqr": [5, 5, 5, 5, None],
            "text": ["a", "b", "c", "d", "e"],
        }
    )

    assert detect_numeric_anomalies(pd.DataFrame()) == []
    assert detect_numeric_anomalies(df) == []
