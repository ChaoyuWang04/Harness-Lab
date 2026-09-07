"""Create the M1 source-of-truth schema."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0001_m1_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("input_json", postgresql.JSONB(), nullable=False),
        sa.Column("result_json", postgresql.JSONB()),
        sa.Column("error_code", sa.String(64)),
        sa.Column("prompt_version", sa.String(32), nullable=False, server_default="v1"),
        sa.Column("lease_owner", sa.Text()),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "run_events",
        sa.Column("run_id", sa.Text(), sa.ForeignKey("agent_runs.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("sequence", sa.Integer(), primary_key=True),
        sa.Column("type", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "outbox_jobs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("task", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_outbox_pending", "outbox_jobs", ["next_attempt_at"], postgresql_where=sa.text("status = 'pending'"))
    op.create_table(
        "tool_calls",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.Text(), sa.ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("step", sa.Integer(), nullable=False),
        sa.Column("tool_name", sa.String(128), nullable=False),
        sa.Column("args_json", postgresql.JSONB(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False, unique=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("result_json", postgresql.JSONB()),
        sa.Column("latency_ms", sa.Integer()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "idempotency_keys",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("run_id", sa.Text(), sa.ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    campaigns = op.create_table(
        "campaigns",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("budget", sa.Numeric(12, 2), nullable=False),
        sa.Column("spend_today", sa.Numeric(12, 2), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
    )
    op.create_table(
        "budget_audit",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("campaign_id", sa.Text(), sa.ForeignKey("campaigns.id"), nullable=False),
        sa.Column("delta", sa.Numeric(12, 2), nullable=False),
        sa.Column("run_id", sa.Text(), sa.ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tool_call_key", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tool_call_key", name="uq_budget_audit_tool_call_key"),
    )
    op.bulk_insert(
        campaigns,
        [
            {"id": "camp_001", "name": "Launch", "budget": 1000, "spend_today": 420, "status": "active"},
            {"id": "camp_002", "name": "Retargeting", "budget": 500, "spend_today": 180, "status": "active"},
            {"id": "camp_003", "name": "Brand", "budget": 2000, "spend_today": 760, "status": "active"},
        ],
    )


def downgrade() -> None:
    for table in ("budget_audit", "campaigns", "idempotency_keys", "tool_calls", "outbox_jobs", "run_events", "agent_runs"):
        op.drop_table(table)
