from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

from sqlalchemy import create_engine, inspect, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateIndex


LAB_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB_ROOT))

from app.config import Settings  # noqa: E402
from app.models import Base, Campaign, OutboxJob  # noqa: E402


class SchemaMetadataTests(unittest.TestCase):
    def test_has_all_m1_tables(self) -> None:
        self.assertEqual(
            set(Base.metadata.tables),
            {
                "agent_runs",
                "run_events",
                "outbox_jobs",
                "tool_calls",
                "idempotency_keys",
                "campaigns",
                "budget_audit",
            },
        )

    def test_required_uniqueness_and_partial_index(self) -> None:
        tool_calls = Base.metadata.tables["tool_calls"]
        self.assertTrue(tool_calls.c.idempotency_key.unique)
        budget_audit = Base.metadata.tables["budget_audit"]
        unique_columns = {
            tuple(column.name for column in constraint.columns)
            for constraint in budget_audit.constraints
            if constraint.__class__.__name__ == "UniqueConstraint"
        }
        self.assertIn(("tool_call_key",), unique_columns)

        ddl = str(CreateIndex(next(iter(OutboxJob.__table__.indexes))).compile(dialect=postgresql.dialect()))
        self.assertIn("WHERE status = 'pending'", ddl)

    def test_timestamps_are_timezone_aware(self) -> None:
        for table_name in ("agent_runs", "run_events", "outbox_jobs", "tool_calls", "budget_audit"):
            column = Base.metadata.tables[table_name].c.created_at
            self.assertTrue(column.type.timezone, table_name)

    def test_settings_separate_container_and_host_database_urls(self) -> None:
        settings = Settings(
            database_url="postgresql+psycopg://postgres:harness@postgres:5432/harness",
            test_database_url="postgresql+psycopg://postgres:harness@127.0.0.1:5432/harness",
        )
        self.assertIn("@postgres:", settings.database_url)
        self.assertIn("@127.0.0.1:", settings.test_database_url)


@unittest.skipUnless(os.getenv("TEST_DATABASE_URL"), "requires Compose PostgreSQL")
class MigratedDatabaseTests(unittest.TestCase):
    def test_migration_and_campaign_seed(self) -> None:
        engine = create_engine(os.environ["TEST_DATABASE_URL"])
        try:
            migrated_tables = set(inspect(engine).get_table_names()) - {"alembic_version"}
            self.assertEqual(migrated_tables, set(Base.metadata.tables))
            with Session(engine) as session:
                campaigns = session.scalars(select(Campaign).order_by(Campaign.id)).all()
                self.assertEqual([item.id for item in campaigns], ["camp_001", "camp_002", "camp_003"])
                self.assertEqual([float(item.budget) for item in campaigns], [1000.0, 500.0, 2000.0])
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
