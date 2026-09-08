from __future__ import annotations

import os
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.orm import Session, sessionmaker


LAB_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB_ROOT))

from app.models import AgentRun, IdempotencyKey, OutboxJob, RunEvent  # noqa: E402
from app import runs as runs_module  # noqa: E402
from app.runs import create_run  # noqa: E402


def test_new_run_stages_run_event_and_outbox_with_one_flush(monkeypatch) -> None:
    staged = []
    flushes = 0

    class RecordingSession:
        def add(self, value) -> None:
            staged.append(value)

        def add_all(self, values) -> None:
            staged.extend(values)

        def flush(self) -> None:
            nonlocal flushes
            flushes += 1

    def stage_legacy_event(session, run_id, event_type, payload):
        event = RunEvent(run_id=run_id, sequence=1, type=event_type, payload=payload)
        session.add(event)
        return event

    monkeypatch.setattr(runs_module, "append_event", stage_legacy_event, raising=False)
    run = create_run(RecordingSession(), {"prompt": "single flush"})

    assert flushes == 1
    assert [type(value) for value in staged] == [AgentRun, RunEvent, OutboxJob]
    assert staged[0] is run
    assert (staged[1].run_id, staged[1].sequence, staged[1].type) == (
        run.id,
        1,
        "run.created",
    )
    assert staged[2].payload == {"run_id": run.id}


@unittest.skipUnless(os.getenv("TEST_DATABASE_URL"), "requires Compose PostgreSQL")
class RunCreationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = create_engine(os.environ["TEST_DATABASE_URL"], pool_pre_ping=True)
        cls.sessions = sessionmaker(cls.engine, expire_on_commit=False)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.dispose()

    def setUp(self) -> None:
        with self.sessions.begin() as session:
            session.execute(delete(IdempotencyKey))
            session.execute(delete(OutboxJob))
            session.execute(delete(RunEvent))
            session.execute(delete(AgentRun))

    def test_one_transaction_creates_run_event_and_outbox(self) -> None:
        with self.sessions.begin() as session:
            run = create_run(session, {"prompt": "diagnose camp_001"})
            run_id = run.id

        with self.sessions() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(AgentRun)), 1)
            event = session.scalar(select(RunEvent).where(RunEvent.run_id == run_id))
            job = session.scalar(select(OutboxJob))
            self.assertEqual((event.sequence, event.type), (1, "run.created"))
            self.assertEqual((job.status, job.task, job.payload), ("pending", "execute_run", {"run_id": run_id}))

    def test_outer_rollback_leaves_no_partial_rows(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "abort"):
            with self.sessions.begin() as session:
                create_run(session, {"prompt": "rollback"})
                raise RuntimeError("abort")

        with self.sessions() as session:
            for model in (AgentRun, RunEvent, OutboxJob):
                self.assertEqual(session.scalar(select(func.count()).select_from(model)), 0)

    def test_same_idempotency_key_concurrently_returns_one_run(self) -> None:
        barrier = threading.Barrier(3)

        def submit() -> str:
            barrier.wait(timeout=5)
            with self.sessions.begin() as session:
                return create_run(
                    session,
                    {"prompt": "same request"},
                    idempotency_key="idem-concurrent-001",
                ).id

        with ThreadPoolExecutor(max_workers=3) as pool:
            run_ids = list(pool.map(lambda _: submit(), range(3)))

        self.assertEqual(len(set(run_ids)), 1)
        with self.sessions() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(AgentRun)), 1)
            self.assertEqual(session.scalar(select(func.count()).select_from(IdempotencyKey)), 1)
            self.assertEqual(session.scalar(select(func.count()).select_from(RunEvent)), 1)
            self.assertEqual(session.scalar(select(func.count()).select_from(OutboxJob)), 1)


if __name__ == "__main__":
    unittest.main()
