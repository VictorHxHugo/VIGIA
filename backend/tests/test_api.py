from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


def test_root_endpoint():
    response = client.get("/")

    assert response.status_code == 200
    assert response.json()["name"] == "VIGIA"


def test_health_endpoint():
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


def test_analyze_valid_csv():
    content = b"id,name\n1,Ana\n2,Bruno\n3,Carlos"

    response = client.post(
        "/datasets/analyze",
        files={
            "file": (
                "customers.csv",
                content,
                "text/csv",
            )
        },
    )

    assert response.status_code == 200

    report = response.json()

    assert report["dataset_name"] == "customers.csv"
    assert report["total_rows"] == 3
    assert report["total_columns"] == 2
    assert report["quality_score"] == 100.0


def test_analyze_rejects_non_csv():
    response = client.post(
        "/datasets/analyze",
        files={
            "file": (
                "customers.xlsx",
                b"invalid content",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )

    assert response.status_code == 400


def test_analyze_rejects_empty_file():
    response = client.post(
        "/datasets/analyze",
        files={
            "file": (
                "empty.csv",
                b"",
                "text/csv",
            )
        },
    )

    assert response.status_code == 400


def test_analyze_rejects_large_file():
    from app.main import MAX_UPLOAD_SIZE

    content = b"x" * (MAX_UPLOAD_SIZE + 1)

    response = client.post(
        "/datasets/analyze",
        files={
            "file": (
                "large.csv",
                content,
                "text/csv",
            )
        },
    )

    assert response.status_code == 413
