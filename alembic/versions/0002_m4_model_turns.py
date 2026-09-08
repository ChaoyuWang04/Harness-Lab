"""Add capture-gated local model turns for M4."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0002_m4_model_turns"
down_revision = "0001_m1_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "model_turns",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "run_id",
            sa.Text(),
            sa.ForeignKey("agent_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("run_attempt", sa.Integer(), nullable=False),
        sa.Column("step", sa.Integer(), nullable=False),
        sa.Column("model_attempt", sa.Integer(), nullable=False),
        sa.Column("prompt_version", sa.String(32), nullable=False),
        sa.Column("input_messages_json", postgresql.JSONB(), nullable=False),
        sa.Column("output_message_json", postgresql.JSONB()),
        sa.Column("usage_json", postgresql.JSONB()),
        sa.Column("error_code", sa.String(32)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "run_id",
            "run_attempt",
            "step",
            "model_attempt",
            name="uq_model_turn_identity",
        ),
    )


def downgrade() -> None:
    op.drop_table("model_turns")
