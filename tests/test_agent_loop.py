from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import sessionmaker


LAB_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB_ROOT))

from app.agent.llm import ModelTurn, OllamaClient, ToolInvocation  # noqa: E402
from app.agent.loop import BadOutput, run_agent  # noqa: E402
from app.agent.tools import ToolError  # noqa: E402
from app.fencing import WorkerFence  # noqa: E402
from app.models import AgentRun, BudgetAudit, Campaign, IdempotencyKey, OutboxJob, RunEvent, ToolCall  # noqa: E402


class SequenceClient:
    def __init__(self, turns: list[ModelTurn]) -> None:
        self.turns = iter(turns)
        self.requests: list[list[dict[str, object]]] = []

    def complete(self, messages, tools):
        self.requests.append([dict(message) for message in messages])
        self.assert_contract(messages, tools)
        return next(self.turns)

    @staticmethod
    def assert_contract(messages, tools) -> None:
        if "/no_think" not in messages[0]["content"] or len(tools) != 3:
            raise AssertionError((messages, tools))


@unittest.skipUnless(os.getenv("TEST_DATABASE_URL"), "requires Compose PostgreSQL")
class AgentLoopTests(unittest.TestCase):
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
            session.get(Campaign, "camp_001").budget = 1000
            session.add(
                AgentRun(
                    id="run_agent_001",
                    status="running",
                    input_json={"prompt": "诊断 camp_001"},
                    lease_owner="worker-a",
                    lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=2),
                    attempt=1,
                )
            )
        self.fence = WorkerFence("worker-a", 1)

    def test_tool_call_then_final_answer_records_ordered_steps(self) -> None:
        client = SequenceClient(
            [
                ModelTurn(
                    content=None,
                    tool_calls=[ToolInvocation("call-1", "get_report", '{"campaign_id":"camp_001"}')],
                ),
                ModelTurn(content="camp_001 今日预算使用率为 42%。", tool_calls=[]),
            ]
        )

        result = run_agent(self.sessions, self.fence, "run_agent_001", "诊断 camp_001", client)

        self.assertEqual(result["answer"], "camp_001 今日预算使用率为 42%。")
        with self.sessions() as session:
            event_types = session.scalars(
                select(RunEvent.type).where(RunEvent.run_id == "run_agent_001").order_by(RunEvent.sequence)
            ).all()
        self.assertEqual(
            event_types,
            ["step.model_call", "step.tool_call", "step.tool_result", "step.model_call"],
        )
        self.assertEqual(len(client.requests), 2)
        self.assertEqual(client.requests[1][-1]["role"], "tool")

    def test_malformed_tool_arguments_are_bad_output(self) -> None:
        client = SequenceClient([ModelTurn(None, [ToolInvocation("call-1", "get_report", "not-json")])])
        with self.assertRaises(BadOutput):
            run_agent(self.sessions, self.fence, "run_agent_001", "x", client)

    def test_unknown_tool_is_rejected(self) -> None:
        client = SequenceClient([ModelTurn(None, [ToolInvocation("call-1", "delete_campaign", "{}")])])
        with self.assertRaises(ToolError):
            run_agent(self.sessions, self.fence, "run_agent_001", "x", client)

    def test_six_tool_rounds_without_final_answer_are_bad_output(self) -> None:
        turns = [
            ModelTurn(None, [ToolInvocation(f"call-{step}", "get_report", '{"campaign_id":"camp_001"}')])
            for step in range(1, 7)
        ]
        with self.assertRaises(BadOutput):
            run_agent(self.sessions, self.fence, "run_agent_001", "x", SequenceClient(turns))

    def test_recovery_attempt_does_not_repeat_gate_pause(self) -> None:
        with self.sessions.begin() as session:
            run = session.get(AgentRun, "run_agent_001")
            run.attempt = 2
        recovery_fence = WorkerFence("worker-a", 2)
        client = SequenceClient(
            [
                ModelTurn(None, [ToolInvocation("call-1", "adjust_budget", '{"campaign_id":"camp_001","delta":100}')]),
                ModelTurn("done", []),
            ]
        )
        with mock.patch("app.agent.loop.time.sleep") as sleeper:
            run_agent(
                self.sessions,
                recovery_fence,
                "run_agent_001",
                "x",
                client,
                pause_after_tool_seconds=45,
            )
        sleeper.assert_not_called()

    @unittest.skipUnless(os.getenv("RUN_LIVE_OLLAMA"), "opt-in live Ollama test")
    def test_live_ollama_completes_campaign_diagnosis(self) -> None:
        result = run_agent(
            self.sessions,
            self.fence,
            "run_agent_001",
            "诊断 camp_001 今日消耗并汇报预算使用率",
            OllamaClient(base_url="http://127.0.0.1:11434/v1", model="qwen3:0.6b"),
        )
        self.assertIn("answer", result)
        self.assertIn("camp_001", result["answer"])


if __name__ == "__main__":
    unittest.main()
