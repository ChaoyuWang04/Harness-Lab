from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session, sessionmaker

from app.api.routes_runs import build_router
from app.api.routes_metrics import build_metrics_router
from app.db import SessionLocal, engine
from app.config import LAB_ROOT
from app.telemetry import initialize_observability, instrument_fastapi


def create_app(session_factory: sessionmaker[Session] = SessionLocal) -> FastAPI:
    initialize_observability("harness-api", engine=engine)
    application = FastAPI(title="Harness Lab", version="0.1.0")
    application.include_router(build_router(session_factory))
    application.include_router(build_metrics_router(session_factory))

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(LAB_ROOT / "web" / "index.html")

    instrument_fastapi(application)
    return application


app = create_app()
