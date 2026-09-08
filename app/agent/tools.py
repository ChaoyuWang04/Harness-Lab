from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agent.policy import ToolPolicy
from app.fencing import WorkerFence, lock_current_run
from app.models import AgentRun, BudgetAudit, Campaign, ToolCall


class ToolError(RuntimeError):
    pass


class ToolResultUnknown(ToolError):
    pass


class BudgetLimitExceeded(ToolError):
    pass


class BudgetIntentMismatch(ToolError):
    pass


class StaleWorkerFence(ToolError):
    pass


@dataclass(frozen=True, slots=True)
class ToolExecution:
    result: dict[str, Any]
    executed_now: bool
    latency_ms: float | None


def _object_schema(properties: dict[str, dict[str, Any]], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "get_campaign",
            "description": "查询广告计划的预算、当日消耗和状态，campaign_id 必须原样保留",
            "parameters": _object_schema({"campaign_id": {"type": "string"}}, ["campaign_id"]),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_report",
            "description": "计算广告计划当日预算使用率，campaign_id 必须原样保留",
            "parameters": _object_schema({"campaign_id": {"type": "string"}}, ["campaign_id"]),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "adjust_budget",
            "description": "按 delta 调整广告计划预算；服务端强制限制绝对值不超过当前预算 20%",
            "parameters": _object_schema(
                {"campaign_id": {"type": "string"}, "delta": {"type": "number"}},
                ["campaign_id", "delta"],
            ),
        },
    },
]


def canonical_tool_key(
    run_id: str,
    step: int,
    tool_name: str,
    arguments: dict[str, Any],
) -> str:
    canonical = json.dumps(arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
    return f"{run_id}:{step}:{tool_name}:{digest}"


def _lock_current_run(session: Session, run_id: str, fence: WorkerFence) -> AgentRun:
    run = lock_current_run(session, run_id, fence)
    if run is None:
        raise StaleWorkerFence("worker lease is no longer current")
    return run


def _campaign_result(campaign: Campaign) -> dict[str, Any]:
    return {
        "campaign_id": campaign.id,
        "name": campaign.name,
        "budget": float(campaign.budget),
        "spend_today": float(campaign.spend_today),
        "status": campaign.status,
    }


def execute_tool_with_outcome(
    session: Session,
    fence: WorkerFence,
    run_id: str,
    step: int,
    tool_name: str,
    arguments: dict[str, Any],
    *,
    policy: ToolPolicy | None = None,
) -> ToolExecution:
    _lock_current_run(session, run_id, fence)
    tool_key = canonical_tool_key(run_id, step, tool_name, arguments)
    existing = session.scalar(select(ToolCall).where(ToolCall.idempotency_key == tool_key))
    if existing is not None:
        if existing.status == "succeeded" and existing.result_json is not None:
            return ToolExecution(existing.result_json, False, None)
        raise ToolResultUnknown("prior tool result is not safely replayable")

    record = ToolCall(
        run_id=run_id,
        step=step,
        tool_name=tool_name,
        args_json=arguments,
        idempotency_key=tool_key,
        status="executing",
    )
    session.add(record)
    session.flush()
    started = time.perf_counter()

    campaign_id = str(arguments.get("campaign_id", ""))
    campaign = session.scalar(
        select(Campaign).where(Campaign.id == campaign_id).with_for_update()
    )
    if campaign is None:
        raise ToolError("campaign not found")

    if tool_name == "get_campaign":
        result = _campaign_result(campaign)
    elif tool_name == "get_report":
        budget = float(campaign.budget)
        result = {
            "campaign_id": campaign.id,
            "budget": budget,
            "spend_today": float(campaign.spend_today),
            "usage_rate": round(float(campaign.spend_today) / budget, 6) if budget else None,
        }
    elif tool_name == "adjust_budget":
        try:
            delta = Decimal(str(arguments["delta"]))
        except (KeyError, ValueError) as exc:
            raise ToolError("delta must be numeric") from exc
        if policy is not None:
            intent = policy.budget_adjustment
            if intent is None:
                raise BudgetIntentMismatch("run has no authorized budget adjustment")
            if intent.campaign_id != campaign.id:
                raise BudgetIntentMismatch("campaign does not match the authorized user intent")
            if delta != intent.resolve(campaign.budget):
                raise BudgetIntentMismatch("delta does not match the complete authorized user intent")
            prior_writes = session.scalar(
                select(func.count())
                .select_from(ToolCall)
                .where(
                    ToolCall.run_id == run_id,
                    ToolCall.tool_name == "adjust_budget",
                    ToolCall.status == "succeeded",
                )
            )
            if prior_writes:
                raise BudgetIntentMismatch("budget adjustment authorization is single-use")
        if abs(delta) > abs(campaign.budget) * Decimal("0.20"):
            raise BudgetLimitExceeded("budget adjustment exceeds 20%")
        campaign.budget += delta
        session.add(
            BudgetAudit(
                campaign_id=campaign.id,
                delta=delta,
                run_id=run_id,
                tool_call_key=tool_key,
            )
        )
        result = {
            "campaign_id": campaign.id,
            "delta": float(delta),
            "budget": float(campaign.budget),
        }
    else:
        raise ToolError("unknown tool")

    record.status = "succeeded"
    record.result_json = result
    latency_ms = (time.perf_counter() - started) * 1000
    record.latency_ms = round(latency_ms)
    session.flush()
    return ToolExecution(result, True, latency_ms)


def execute_tool(
    session: Session,
    fence: WorkerFence,
    run_id: str,
    step: int,
    tool_name: str,
    arguments: dict[str, Any],
    *,
    policy: ToolPolicy | None = None,
) -> dict[str, Any]:
    return execute_tool_with_outcome(
        session,
        fence,
        run_id,
        step,
        tool_name,
        arguments,
        policy=policy,
    ).result
