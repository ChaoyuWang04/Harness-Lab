from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.orm import sessionmaker


LAB_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB_ROOT))

from app.agent.tools import (  # noqa: E402
    TOOL_SCHEMAS,
    BudgetLimitExceeded,
    canonical_tool_key,
    execute_tool,
)
from app.fencing import WorkerFence  # noqa: E402
from app.models import AgentRun, BudgetAudit, Campaign, IdempotencyKey, OutboxJob, RunEvent, ToolCall  # noqa: E402


class ToolContractTests(unittest.TestCase):
    def test_exposes_exactly_three_bounded_tool_schemas(self) -> None:
        names = [item["function"]["name"] for item in TOOL_SCHEMAS]
        self.assertEqual(names, ["get_campaign", "get_report", "adjust_budget"])
        adjust = TOOL_SCHEMAS[2]["function"]["parameters"]
        self.assertEqual(adjust["required"], ["campaign_id", "delta"])
        self.assertFalse(adjust["additionalProperties"])

    def test_canonical_key_ignores_argument_order(self) -> None:
        first = canonical_tool_key("run_1", 2, "adjust_budget", {"campaign_id": "camp_001", "delta": 10})
        second = canonical_tool_key("run_1", 2, "adjust_budget", {"delta": 10, "campaign_id": "camp_001"})
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("run_1:2:adjust_budget:"))


@unittest.skipUnless(os.getenv("TEST_DATABASE_URL"), "requires Compose PostgreSQL")
class ToolExecutionTests(unittest.TestCase):
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
            campaign = session.get(Campaign, "camp_001")
            campaign.budget = Decimal("1000.00")
        self.fence = WorkerFence(owner="worker-a", attempt=1)
        with self.sessions.begin() as session:
            session.add(
                AgentRun(
                    id="run_tools_001",
                    status="running",
                    input_json={"prompt": "x"},
                    lease_owner=self.fence.owner,
                    lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=1),
                    attempt=self.fence.attempt,
                )
            )

    def tearDown(self) -> None:
        with self.sessions.begin() as session:
            session.get(Campaign, "camp_001").budget = Decimal("1000.00")

    def test_read_tools_return_exact_campaign_and_deterministic_report(self) -> None:
        with self.sessions.begin() as session:
            campaign = execute_tool(session, self.fence, "run_tools_001", 1, "get_campaign", {"campaign_id": "camp_001"})
            report = execute_tool(session, self.fence, "run_tools_001", 2, "get_report", {"campaign_id": "camp_001"})
        self.assertEqual(campaign["campaign_id"], "camp_001")
        self.assertEqual(report["usage_rate"], 0.42)

    def test_duplicate_adjust_budget_changes_and_audits_once(self) -> None:
        arguments = {"campaign_id": "camp_001", "delta": 100}
        with self.sessions.begin() as session:
            first = execute_tool(session, self.fence, "run_tools_001", 3, "adjust_budget", arguments)
        with self.sessions.begin() as session:
            replay = execute_tool(session, self.fence, "run_tools_001", 3, "adjust_budget", arguments)

        self.assertEqual(first, replay)
        with self.sessions() as session:
            self.assertEqual(session.get(Campaign, "camp_001").budget, Decimal("1100.00"))
            self.assertEqual(session.scalar(select(func.count()).select_from(BudgetAudit)), 1)
            self.assertEqual(session.scalar(select(func.count()).select_from(ToolCall)), 1)

    def test_rejects_adjustment_over_twenty_percent_without_side_effect(self) -> None:
        with self.assertRaises(BudgetLimitExceeded):
            with self.sessions.begin() as session:
                execute_tool(
                    session,
                    self.fence,
                    "run_tools_001",
                    3,
                    "adjust_budget",
                    {"campaign_id": "camp_001", "delta": 201},
                )
        with self.sessions() as session:
            self.assertEqual(session.get(Campaign, "camp_001").budget, Decimal("1000.00"))
            self.assertEqual(session.scalar(select(func.count()).select_from(BudgetAudit)), 0)


if __name__ == "__main__":
    unittest.main()
