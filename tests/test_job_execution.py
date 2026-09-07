from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

import httpx2
from openai import APITimeoutError, RateLimitError

from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import sessionmaker


LAB_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB_ROOT))

from app.agent.llm import ModelTurn, ToolInvocation  # noqa: E402
from app.jobs import classify_error, execute_run  # noqa: E402
from app.models import AgentRun, BudgetAudit, IdempotencyKey, OutboxJob, RunEvent, ToolCall  # noqa: E402


class SequenceClient:
    def __init__(self, turns):
        self.turns = iter(turns)

    def complete(self, _messages, _tools):
        return next(self.turns)


class ErrorClassificationTests(unittest.TestCase):
    def test_model_timeout_and_rate_limit_have_stable_codes(self) -> None:
        request = httpx2.Request("POST", "http://model.invalid/v1/chat/completions")
        response = httpx2.Response(429, request=request)
        self.assertEqual(classify_error(APITimeoutError(request)), "MODEL_TIMEOUT")
        self.assertEqual(
            classify_error(RateLimitError("limited", response=response, body=None)),
            "MODEL_429",
        )


@unittest.skipUnless(os.getenv("TEST_DATABASE_URL"), "requires Compose PostgreSQL")
class JobExecutionTests(unittest.TestCase):
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
            session.add(AgentRun(id="run_job_001", status="queued", input_json={"prompt": "诊断 camp_001"}))

    def test_job_claims_runs_agent_and_finishes_once(self) -> None:
        client = SequenceClient(
            [
                ModelTurn(None, [ToolInvocation("call-1", "get_report", '{"campaign_id":"camp_001"}')]),
                ModelTurn("camp_001 使用率 42%", []),
            ]
        )
        self.assertTrue(
            execute_run(
                "run_job_001",
                session_factory=self.sessions,
                client=client,
                worker_id="worker-test",
                enable_renewer=False,
            )
        )
        self.assertFalse(
            execute_run(
                "run_job_001",
                session_factory=self.sessions,
                client=client,
                worker_id="worker-other",
                enable_renewer=False,
            )
        )
        with self.sessions() as session:
            run = session.get(AgentRun, "run_job_001")
            self.assertEqual(run.status, "completed")
            self.assertEqual(run.result_json, {"answer": "camp_001 使用率 42%"})
            self.assertIsNone(run.lease_owner)
            event_types = session.scalars(
                select(RunEvent.type).where(RunEvent.run_id == run.id).order_by(RunEvent.sequence)
            ).all()
            self.assertEqual(
                event_types,
                ["run.started", "step.model_call", "step.tool_call", "step.tool_result", "step.model_call", "run.completed"],
            )

    def test_gate_pause_occurs_after_committed_tool_result(self) -> None:
        client = SequenceClient(
            [
                ModelTurn(None, [ToolInvocation("call-1", "adjust_budget", '{"campaign_id":"camp_001","delta":100}')]),
                ModelTurn("done", []),
            ]
        )
        observed_event = []

        def assert_result_committed(_seconds: float) -> None:
            with self.sessions() as session:
                observed_event.extend(
                    session.scalars(
                        select(RunEvent.type).where(
                            RunEvent.run_id == "run_job_001",
                            RunEvent.type == "step.tool_result",
                        )
                    ).all()
                )

        with mock.patch("app.agent.loop.time.sleep", side_effect=assert_result_committed) as sleeper:
            execute_run(
                "run_job_001",
                session_factory=self.sessions,
                client=client,
                worker_id="worker-test",
                enable_renewer=False,
                pause_after_tool_seconds=7,
            )
        sleeper.assert_called_once_with(7)
        self.assertEqual(observed_event, ["step.tool_result"])

    def test_gate_pause_does_not_delay_read_tools(self) -> None:
        client = SequenceClient(
            [
                ModelTurn(None, [ToolInvocation("call-1", "get_report", '{"campaign_id":"camp_001"}')]),
                ModelTurn("done", []),
            ]
        )
        with mock.patch("app.agent.loop.time.sleep") as sleeper:
            execute_run(
                "run_job_001",
                session_factory=self.sessions,
                client=client,
                worker_id="worker-test",
                enable_renewer=False,
                pause_after_tool_seconds=7,
            )
        sleeper.assert_not_called()


if __name__ == "__main__":
    unittest.main()
