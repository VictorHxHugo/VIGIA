import pandas as pd

from app.quality.engine import analyze_dataset


def test_quality_engine_detects_data_issues():
    df = pd.DataFrame(
        {
            "name": ["Ana", "Bruno", None, "Daniel", "Daniel"],
            "email": ["ana@email.com", None, "carlos@email.com", None, None],
            "age": [25, 32, 28, None, None],
        }
    )

    report = analyze_dataset(df, "test_customers")

    assert report.total_rows == 5
    assert report.total_columns == 3
    assert report.missing_cells == 6
    assert report.duplicate_rows == 1

    issue_codes = {issue.code for issue in report.issues}

    assert "MISSING_VALUES" in issue_codes
    assert report.quality_score < 100


def test_quality_engine_detects_duplicate_rows():
    df = pd.DataFrame(
        {
            "id": [1, 2, 2],
            "name": ["Ana", "Bruno", "Bruno"],
        }
    )

    report = analyze_dataset(df, "duplicate_test")

    assert report.duplicate_rows == 1

    issue_codes = {issue.code for issue in report.issues}

    assert "DUPLICATE_ROWS" in issue_codes
