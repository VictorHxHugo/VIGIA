from datetime import datetime, timezone

from fastapi import FastAPI, File, HTTPException, UploadFile

from app.ingestion.csv_reader import (
    CSVValidationError,
    read_csv_file,
)
from app.quality.engine import analyze_dataset


app = FastAPI(
    title="VIGIA API",
    description="Data observability and reliability platform.",
    version="0.1.0",
)

MAX_UPLOAD_SIZE = 10 * 1024 * 1024  # 10 MB


@app.get("/")
def root():
    return {
        "name": "VIGIA",
        "description": "Data observability platform",
        "version": "0.1.0",
        "status": "operational",
    }


@app.get("/health")
def health_check():
    return {
        "status": "healthy",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.post("/datasets/analyze")
async def analyze_csv(file: UploadFile = File(...)):
    try:
        if not file.filename:
            raise HTTPException(
                status_code=400,
                detail="Filename is required.",
            )

        content = await file.read(MAX_UPLOAD_SIZE + 1)

        if len(content) > MAX_UPLOAD_SIZE:
            raise HTTPException(
                status_code=413,
                detail="File exceeds the 10 MB upload limit.",
            )

        df = read_csv_file(content, file.filename)

        report = analyze_dataset(
            df,
            dataset_name=file.filename,
        )

        return report.model_dump()

    except CSVValidationError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        ) from exc

    except HTTPException:
        raise

    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail="An unexpected error occurred during dataset analysis.",
        ) from exc

    finally:
        await file.close()
