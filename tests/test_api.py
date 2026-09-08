from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.orm import sessionmaker


LAB_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB_ROOT))

from app.api.main import create_app  # noqa: E402
from app.models import AgentRun, IdempotencyKey, ModelTurnRecord, OutboxJob, RunEvent  # noqa: E402


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
            session.execute(delete(ModelTurnRecord))
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

    def test_public_run_and_ui_never_expose_captured_model_turns(self) -> None:
        sentinel = "M4_PRIVATE_SENTINEL_7f93"
        with self.sessions.begin() as session:
            session.add(
                AgentRun(
                    id="run_private_m4",
                    status="completed",
                    input_json={"prompt": "public prompt"},
                    result_json={"answer": "public answer"},
                )
            )
            session.flush()
            session.add(
                ModelTurnRecord(
                    run_id="run_private_m4",
                    run_attempt=1,
                    step=1,
                    model_attempt=0,
                    prompt_version="v1",
                    input_messages_json=[{"role": "system", "content": sentinel}],
                    output_message_json={"content": sentinel, "tool_calls": []},
                    usage_json={"private_usage": sentinel},
                    error_code="MODEL_INTERNAL",
                )
            )

        combined = self.client.get("/runs/run_private_m4").text + self.client.get("/").text
        self.assertNotIn(sentinel, combined)
        for private_key in ("model_turns", "input_messages_json", "output_message_json"):
            self.assertNotIn(private_key, combined)


if __name__ == "__main__":
    unittest.main()
