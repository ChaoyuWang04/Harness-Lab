from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.orm import sessionmaker


LAB_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB_ROOT))

from app.api.main import create_app  # noqa: E402
from app.db import SessionLocal, warm_engine_pool  # noqa: E402
from app.models import AgentRun, IdempotencyKey, OutboxJob, RunEvent  # noqa: E402


def test_warm_engine_pool_holds_target_connections_before_releasing() -> None:
    state = {"active": 0, "peak": 0, "closed": 0}

    class FakeConnection:
        def close(self) -> None:
            state["active"] -= 1
            state["closed"] += 1

    class FakeEngine:
        def connect(self) -> FakeConnection:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
            return FakeConnection()

    assert warm_engine_pool(FakeEngine(), 10) == 10
    assert state == {"active": 0, "peak": 10, "closed": 10}


def test_api_lifespan_prewarms_the_bound_pool() -> None:
    with patch("app.api.main.warm_engine_pool") as warm:
        with TestClient(create_app(pool_warm_connections=3)) as client:
            assert client.get("/health").status_code == 200

    warm.assert_called_once_with(SessionLocal.kw["bind"], 3)


@unittest.skipUnless(os.getenv("TEST_DATABASE_URL"), "requires Compose PostgreSQL")
class RunsApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = create_engine(os.environ["TEST_DATABASE_URL"], pool_pre_ping=True)
        cls.sessions = sessionmaker(cls.engine, expire_on_commit=False)
        cls.client = TestClient(create_app(cls.sessions))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.close()
        cls.engine.dispose()

    def setUp(self) -> None:
        with self.sessions.begin() as session:
            session.execute(delete(IdempotencyKey))
            session.execute(delete(OutboxJob))
            session.execute(delete(RunEvent))
            session.execute(delete(AgentRun))

    def test_post_and_get_run_without_redis(self) -> None:
        response = self.client.post("/runs", json={"prompt": "diagnose camp_001"})
        self.assertEqual(response.status_code, 201, response.text)
        run_id = response.json()["run_id"]
        self.assertTrue(run_id.startswith("run_"))

        fetched = self.client.get(f"/runs/{run_id}")
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched.json()["status"], "queued")
        with self.sessions() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(OutboxJob)), 1)

    def test_idempotency_header_returns_same_run(self) -> None:
        ids = [
            self.client.post(
                "/runs",
                json={"prompt": "same"},
                headers={"Idempotency-Key": "api-idem-001"},
            ).json()["run_id"]
            for _ in range(3)
        ]
        self.assertEqual(len(set(ids)), 1)
        with self.sessions() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(AgentRun)), 1)

    def test_unknown_run_is_404(self) -> None:
        self.assertEqual(self.client.get("/runs/run_missing").status_code, 404)

    def test_root_serves_eventsource_viewer(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("EventSource", response.text)
        self.assertIn("Last-Event-ID", response.text)


if __name__ == "__main__":
    unittest.main()
