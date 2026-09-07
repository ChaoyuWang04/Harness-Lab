from __future__ import annotations

from fastapi import FastAPI


def create_app(*, fault_mode: str | None = None) -> FastAPI:
    if fault_mode:
        raise ValueError("fault injection is disabled until M3")
    application = FastAPI(title="Harness Lab Chaos Placeholder")

    @application.get("/health")
    def health() -> dict[str, str | bool]:
        return {"status": "placeholder", "faults_enabled": False}

    return application


app = create_app()
