from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session, sessionmaker

from app.api.routes_runs import build_router
from app.api.routes_metrics import build_metrics_router
from app.db import SessionLocal, engine, warm_engine_pool
from app.config import LAB_ROOT, settings
from app.telemetry import initialize_observability, instrument_fastapi


def create_app(
    session_factory: sessionmaker[Session] = SessionLocal,
    *,
    pool_warm_connections: int | None = None,
) -> FastAPI:
    initialize_observability("harness-api", engine=engine)
    warm_count = (
        settings.database_pool_warm_connections
        if pool_warm_connections is None
        else pool_warm_connections
    )
    bound_engine = session_factory.kw.get("bind")

    @asynccontextmanager
    async def lifespan(_application: FastAPI) -> AsyncIterator[None]:
        if warm_count and bound_engine is not None:
            warm_engine_pool(bound_engine, warm_count)
        yield

    application = FastAPI(title="Harness Lab", version="0.1.0", lifespan=lifespan)
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
