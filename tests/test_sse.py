from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete
from sqlalchemy.orm import sessionmaker


LAB_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB_ROOT))

from app.api.main import create_app  # noqa: E402
from app.events import append_event  # noqa: E402
from app.models import AgentRun, IdempotencyKey, OutboxJob, RunEvent  # noqa: E402


@unittest.skipUnless(os.getenv("TEST_DATABASE_URL"), "requires Compose PostgreSQL")
class SseReplayTests(unittest.TestCase):
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

    def test_last_event_id_replays_next_events_once_and_closes(self) -> None:
        with self.sessions.begin() as session:
            run = AgentRun(id="run_sse_001", status="completed", input_json={"prompt": "x"})
            session.add(run)
            session.flush()
            append_event(session, run.id, "run.created")
            append_event(session, run.id, "run.started")
            append_event(session, run.id, "run.completed", {"answer": "done"})

        response = self.client.get(
            "/runs/run_sse_001/events",
            headers={"Last-Event-ID": "1"},
        )
        self.assertEqual(response.status_code, 200)
        ids = [int(line[4:]) for line in response.text.splitlines() if line.startswith("id: ")]
        events = [line[7:] for line in response.text.splitlines() if line.startswith("event: ")]
        data = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        self.assertEqual(ids, [2, 3])
        self.assertEqual(events, ["run.started", "run.completed"])
        self.assertEqual(data[-1], {"answer": "done"})

    def test_invalid_last_event_id_is_rejected(self) -> None:
        response = self.client.get(
            "/runs/run_missing/events",
            headers={"Last-Event-ID": "not-an-int"},
        )
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
