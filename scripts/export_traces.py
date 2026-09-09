from __future__ import annotations

import argparse
import json
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.eval.export import export_trajectories
from app.models import AgentRun, BudgetAudit, ModelTurnRecord, RunEvent, ToolCall


def extract_database_fixtures(
    database_url: str,
    cohort_manifest: dict[str, object],
) -> list[dict[str, object]]:
    engine = create_engine(database_url, pool_pre_ping=True)
    fixtures: list[dict[str, object]] = []
    try:
        with engine.connect().execution_options(isolation_level="REPEATABLE READ") as connection:
            transaction = connection.begin()
            connection.exec_driver_sql("SET TRANSACTION READ ONLY")
            session = Session(bind=connection)
            try:
                for case_record in cohort_manifest["cases"]:
                    run_id = case_record["run_id"]
                    run = session.get(AgentRun, run_id)
                    if run is None:
                        raise RuntimeError(f"cohort run not found: {run_id}")
                    turns = session.scalars(
                        select(ModelTurnRecord)
                        .where(ModelTurnRecord.run_id == run_id)
                        .order_by(
                            ModelTurnRecord.run_attempt,
                            ModelTurnRecord.step,
                            ModelTurnRecord.model_attempt,
                        )
                    ).all()
                    events = session.scalars(
                        select(RunEvent)
                        .where(RunEvent.run_id == run_id)
                        .order_by(RunEvent.sequence)
                    ).all()
                    tools = session.scalars(
                        select(ToolCall).where(ToolCall.run_id == run_id).order_by(ToolCall.id)
                    ).all()
                    audits = session.scalars(
                        select(BudgetAudit)
                        .where(BudgetAudit.run_id == run_id)
                        .order_by(BudgetAudit.id)
                    ).all()
                    fixtures.append(
                        {
                            "case": {
                                "case_id": case_record["case_id"],
                                "scenario_kind": case_record["scenario_kind"],
                                "world_fixture_id": case_record["world_fixture_id"],
                                "fault_schedule_id": case_record["fault_schedule_id"],
                                "expected_behavior": case_record["expected_behavior"],
                            },
                            "run": {
                                "id": run.id,
                                "status": run.status,
                                "input_json": run.input_json,
                                "result_json": run.result_json,
                                "error_code": run.error_code,
                                "prompt_version": run.prompt_version,
                            },
                            "model_turns": [
                                {
                                    "run_id": item.run_id,
                                    "run_attempt": item.run_attempt,
                                    "step": item.step,
                                    "model_attempt": item.model_attempt,
                                    "prompt_version": item.prompt_version,
                                    "input_messages_json": item.input_messages_json,
                                    "output_message_json": item.output_message_json,
                                    "usage_json": item.usage_json,
                                    "error_code": item.error_code,
                                }
                                for item in turns
                            ],
                            "events": [
                                {"sequence": item.sequence, "type": item.type, "payload": item.payload}
                                for item in events
                            ],
                            "tool_calls": [
                                {
                                    "step": item.step,
                                    "tool_name": item.tool_name,
                                    "args_json": item.args_json,
                                    "status": item.status,
                                    "result_json": item.result_json,
                                    "idempotency_key": item.idempotency_key,
                                }
                                for item in tools
                            ],
                            "audits": [
                                {
                                    "campaign_id": item.campaign_id,
                                    "delta": float(item.delta),
                                    "tool_call_key": item.tool_call_key,
                                }
                                for item in audits
                            ],
                        }
                    )
            finally:
                session.close()
                transaction.rollback()
    finally:
        engine.dispose()
    return fixtures


def main() -> None:
    parser = argparse.ArgumentParser(description="Build immutable M4 raw trajectories")
    parser.add_argument("--database-url")
    parser.add_argument("--cohort-manifest", type=Path)
    parser.add_argument("--extracted-json", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.extracted_json is not None:
        fixtures = json.loads(args.extracted_json.read_text(encoding="utf-8"))
    elif args.database_url and args.cohort_manifest:
        cohort = json.loads(args.cohort_manifest.read_text(encoding="utf-8"))
        fixtures = extract_database_fixtures(args.database_url, cohort)
    else:
        raise SystemExit("provide --database-url with --cohort-manifest, or --extracted-json")
    if not isinstance(fixtures, list):
        raise SystemExit("extracted input must be a JSON list")
    export_trajectories(fixtures, args.output_dir)


if __name__ == "__main__":
    main()
