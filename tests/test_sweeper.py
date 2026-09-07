from __future__ import annotations

import os
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.orm import sessionmaker


LAB_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB_ROOT))

from app.models import AgentRun, BudgetAudit, IdempotencyKey, OutboxJob, RunEvent, ToolCall  # noqa: E402
from app.sweeper import sweep_once  # noqa: E402


@unittest.skipUnless(os.getenv("TEST_DATABASE_URL"), "requires Compose PostgreSQL")
class SweeperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = create_engine(os.environ["TEST_DATABASE_URL"], pool_pre_ping=True)
        cls.sessions = sessionmaker(cls.engine, expire_on_commit=False)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.dispose()

    def setUp(self) -> None:
        with self.sessions.begin() as session:
            session.execute(delete(BudgetAudit))
            session.execute(delete(ToolCall))
            session.execute(delete(IdempotencyKey))
            session.execute(delete(OutboxJob))
            session.execute(delete(RunEvent))
            session.execute(delete(AgentRun))

    def add_running(self, run_id: str, *, expired_seconds: int, attempt: int) -> None:
        with self.sessions.begin() as session:
            session.add(
                AgentRun(
                    id=run_id,
                    status="running",
                    input_json={"prompt": "x"},
                    lease_owner="dead-worker",
                    lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=expired_seconds),
                    attempt=attempt,
                )
            )

    def test_five_second_grace_then_requeues_through_outbox(self) -> None:
        self.add_running("run_grace", expired_seconds=4, attempt=1)
        self.assertEqual(sweep_once(self.sessions, max_attempts=3), 0)
        with self.sessions() as session:
            self.assertEqual(session.get(AgentRun, "run_grace").status, "running")

        with self.sessions.begin() as session:
            session.get(AgentRun, "run_grace").lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=6)
        self.assertEqual(sweep_once(self.sessions, max_attempts=3), 1)
        with self.sessions() as session:
            run = session.get(AgentRun, "run_grace")
            self.assertEqual((run.status, run.lease_owner, run.attempt), ("queued", None, 1))
            self.assertEqual(session.scalar(select(func.count()).select_from(OutboxJob)), 1)
            self.assertEqual(
                session.scalar(select(func.count()).select_from(RunEvent).where(RunEvent.type == "run.requeued_by_sweeper")),
                1,
            )

    def test_attempt_three_fails_without_new_outbox(self) -> None:
        self.add_running("run_max", expired_seconds=10, attempt=3)
        self.assertEqual(sweep_once(self.sessions, max_attempts=3), 1)
        with self.sessions() as session:
            run = session.get(AgentRun, "run_max")
            self.assertEqual((run.status, run.error_code), ("failed", "MAX_RETRY"))
            self.assertEqual(session.scalar(select(func.count()).select_from(OutboxJob)), 0)
            self.assertEqual(
                session.scalar(select(func.count()).select_from(RunEvent).where(RunEvent.type == "run.failed")),
                1,
            )

    def test_requeue_preserves_original_trace_carrier(self) -> None:
        self.add_running("run_trace_retry", expired_seconds=10, attempt=1)
        carrier = {
            "traceparent": "00-11111111111111111111111111111111-2222222222222222-01"
        }
        with self.sessions.begin() as session:
            session.add(
                OutboxJob(
                    task="execute_run",
                    status="dispatched",
                    payload={"run_id": "run_trace_retry", "trace_context": carrier},
                )
            )

        self.assertEqual(sweep_once(self.sessions, max_attempts=3), 1)

        with self.sessions() as session:
            recovered = session.scalars(
                select(OutboxJob)
                .where(OutboxJob.status == "pending")
                .order_by(OutboxJob.id.desc())
            ).first()
            self.assertIsNotNone(recovered)
            self.assertEqual(recovered.payload["trace_context"], carrier)

    def test_competing_sweepers_transition_once(self) -> None:
        self.add_running("run_race", expired_seconds=10, attempt=1)
        with ThreadPoolExecutor(max_workers=2) as pool:
            counts = list(pool.map(lambda _: sweep_once(self.sessions, max_attempts=3), range(2)))
        self.assertEqual(sum(counts), 1)
        with self.sessions() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(OutboxJob)), 1)
            self.assertEqual(session.scalar(select(func.count()).select_from(RunEvent)), 1)


if __name__ == "__main__":
    unittest.main()
