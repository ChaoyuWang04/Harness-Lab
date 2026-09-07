from __future__ import annotations

import os
import re
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

from rq.exceptions import DuplicateJobError
from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.orm import sessionmaker


LAB_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB_ROOT))

from app.models import AgentRun, IdempotencyKey, OutboxJob, RunEvent  # noqa: E402
from app.outbox import dispatch_batch  # noqa: E402
from app.runs import create_run  # noqa: E402


class RecordingQueue:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.deliveries: list[tuple[str, str]] = []

    def enqueue_call(
        self,
        _func: str,
        *,
        args: list[str],
        job_id: str,
        unique: bool,
        meta: dict[str, object],
    ) -> object:
        self.assert_contract(args, unique)
        self.assert_delivery_id(job_id)
        if meta.get("run_id") != args[0] or "trace_context" not in meta:
            raise AssertionError(meta)
        with self.lock:
            if any(existing_id == job_id for existing_id, _ in self.deliveries):
                raise DuplicateJobError(job_id)
            self.deliveries.append((job_id, args[0]))
        return object()

    @staticmethod
    def assert_contract(args: list[str], unique: bool) -> None:
        if len(args) != 1 or not unique:
            raise AssertionError((args, unique))

    @staticmethod
    def assert_delivery_id(delivery_id: str) -> None:
        if re.fullmatch(r"[A-Za-z0-9_-]+", delivery_id) is None:
            raise AssertionError(delivery_id)


class FailingQueue:
    def enqueue_call(self, *_args, **_kwargs) -> object:
        raise ConnectionError("redis unavailable")


@unittest.skipUnless(os.getenv("TEST_DATABASE_URL"), "requires Compose PostgreSQL")
class OutboxTests(unittest.TestCase):
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

    def test_two_dispatchers_claim_each_outbox_row_once(self) -> None:
        with self.sessions.begin() as session:
            run_ids = [create_run(session, {"prompt": f"run {index}"}).id for index in range(20)]
        queue = RecordingQueue()

        with ThreadPoolExecutor(max_workers=2) as pool:
            dispatched = list(pool.map(lambda _: dispatch_batch(self.sessions, queue, limit=100), range(2)))

        self.assertEqual(sum(dispatched), 20)
        self.assertEqual({run_id for _, run_id in queue.deliveries}, set(run_ids))
        self.assertEqual(len({delivery_id for delivery_id, _ in queue.deliveries}), 20)
        with self.sessions() as session:
            self.assertEqual(
                session.scalar(select(func.count()).select_from(OutboxJob).where(OutboxJob.status == "dispatched")),
                20,
            )
            self.assertEqual(
                session.scalar(select(func.count()).select_from(RunEvent).where(RunEvent.type == "run.enqueued")),
                20,
            )

    def test_failure_stays_pending_with_exponential_retry(self) -> None:
        with self.sessions.begin() as session:
            run_id = create_run(session, {"prompt": "retry"}).id
        with self.sessions() as session:
            eligible_at = session.scalar(select(OutboxJob.next_attempt_at))

        fixed_now = eligible_at + timedelta(seconds=10)
        self.assertEqual(dispatch_batch(self.sessions, FailingQueue(), now=fixed_now), 0)

        with self.sessions() as session:
            job = session.scalar(select(OutboxJob).where(OutboxJob.payload["run_id"].astext == run_id))
            self.assertEqual(job.status, "pending")
            self.assertEqual(job.attempts, 1)
            self.assertEqual((job.next_attempt_at - fixed_now).total_seconds(), 1)
            self.assertEqual(job.last_error, "ConnectionError")
            self.assertEqual(
                session.scalar(select(func.count()).select_from(RunEvent).where(RunEvent.type == "run.enqueued")),
                0,
            )

    def test_recovery_outbox_for_same_run_gets_a_new_delivery_id(self) -> None:
        with self.sessions.begin() as session:
            run_id = create_run(session, {"prompt": "recover"}).id
            session.add(OutboxJob(task="execute_run", payload={"run_id": run_id}))
        queue = RecordingQueue()

        self.assertEqual(dispatch_batch(self.sessions, queue), 2)

        self.assertEqual([run for _, run in queue.deliveries], [run_id, run_id])
        self.assertEqual(len({delivery for delivery, _ in queue.deliveries}), 2)

    def test_post_publish_crash_rolls_back_outbox_and_retry_deduplicates_delivery(self) -> None:
        with self.sessions.begin() as session:
            run_id = create_run(session, {"prompt": "dispatcher crash"}).id
        queue = RecordingQueue()

        with self.assertRaisesRegex(RuntimeError, "crash after publish"):
            dispatch_batch(
                self.sessions,
                queue,
                post_publish_hook=lambda _run_id, _outbox_id: (_ for _ in ()).throw(
                    RuntimeError("crash after publish")
                ),
            )

        with self.sessions() as session:
            job = session.scalar(select(OutboxJob).where(OutboxJob.payload["run_id"].astext == run_id))
            self.assertEqual(job.status, "pending")
            self.assertEqual(
                session.scalar(
                    select(func.count()).select_from(RunEvent).where(RunEvent.type == "run.enqueued")
                ),
                0,
            )

        self.assertEqual(dispatch_batch(self.sessions, queue), 1)
        self.assertEqual(len(queue.deliveries), 1)
        with self.sessions() as session:
            job = session.scalar(select(OutboxJob).where(OutboxJob.payload["run_id"].astext == run_id))
            self.assertEqual(job.status, "dispatched")
            self.assertEqual(
                session.scalar(
                    select(func.count()).select_from(RunEvent).where(RunEvent.type == "run.enqueued")
                ),
                1,
            )


if __name__ == "__main__":
    unittest.main()
