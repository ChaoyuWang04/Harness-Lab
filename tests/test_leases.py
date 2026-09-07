from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine, delete, func, select, update
from sqlalchemy.orm import sessionmaker


LAB_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB_ROOT))

from app.agent.tools import StaleWorkerFence, execute_tool  # noqa: E402
from app.events import append_event  # noqa: E402
from app.leases import claim_run, finish_run, renew_lease  # noqa: E402
from app.models import AgentRun, BudgetAudit, Campaign, IdempotencyKey, OutboxJob, RunEvent, ToolCall  # noqa: E402


@unittest.skipUnless(os.getenv("TEST_DATABASE_URL"), "requires Compose PostgreSQL")
class LeaseFenceTests(unittest.TestCase):
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
            session.get(Campaign, "camp_001").budget = Decimal("1000.00")
            session.add(AgentRun(id="run_lease_001", status="queued", input_json={"prompt": "x"}))

    def tearDown(self) -> None:
        with self.sessions.begin() as session:
            session.get(Campaign, "camp_001").budget = Decimal("1000.00")

    def test_claim_refuses_live_lease_and_renewal_keeps_same_fence(self) -> None:
        first = claim_run(self.sessions, "run_lease_001", "worker-a", lease_seconds=30)
        self.assertEqual((first.owner, first.attempt), ("worker-a", 1))
        self.assertIsNone(claim_run(self.sessions, "run_lease_001", "worker-b", lease_seconds=30))
        self.assertTrue(renew_lease(self.sessions, "run_lease_001", first, lease_seconds=30))
        with self.sessions() as session:
            run = session.get(AgentRun, "run_lease_001")
            self.assertEqual((run.lease_owner, run.attempt, run.status), ("worker-a", 1, "running"))

    def test_stale_worker_cannot_write_after_new_claim(self) -> None:
        stale = claim_run(self.sessions, "run_lease_001", "worker-a", lease_seconds=30)
        with self.sessions.begin() as session:
            session.execute(
                update(AgentRun)
                .where(AgentRun.id == "run_lease_001")
                .values(lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=10))
            )
        current = claim_run(self.sessions, "run_lease_001", "worker-b", lease_seconds=30)
        self.assertEqual((current.owner, current.attempt), ("worker-b", 2))

        self.assertFalse(renew_lease(self.sessions, "run_lease_001", stale, lease_seconds=30))
        with self.assertRaises(PermissionError):
            with self.sessions.begin() as session:
                append_event(session, "run_lease_001", "step.model_call", fence=stale)
        with self.assertRaises(StaleWorkerFence):
            with self.sessions.begin() as session:
                execute_tool(
                    session,
                    stale,
                    "run_lease_001",
                    1,
                    "adjust_budget",
                    {"campaign_id": "camp_001", "delta": 100},
                )
        self.assertFalse(finish_run(self.sessions, "run_lease_001", stale, status="completed", result={"answer": "stale"}))

        with self.sessions.begin() as session:
            execute_tool(
                session,
                current,
                "run_lease_001",
                1,
                "adjust_budget",
                {"campaign_id": "camp_001", "delta": 100},
            )
        self.assertTrue(finish_run(self.sessions, "run_lease_001", current, status="completed", result={"answer": "ok"}))

        with self.sessions() as session:
            self.assertEqual(session.get(Campaign, "camp_001").budget, Decimal("1100.00"))
            self.assertEqual(session.scalar(select(func.count()).select_from(BudgetAudit)), 1)
            self.assertEqual(
                session.scalar(select(func.count()).select_from(RunEvent).where(RunEvent.type == "run.completed")),
                1,
            )


if __name__ == "__main__":
    unittest.main()
