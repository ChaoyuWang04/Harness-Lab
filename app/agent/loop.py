from __future__ import annotations

import json
import time
from typing import Any

from openai import APITimeoutError, RateLimitError
from opentelemetry import trace
from sqlalchemy.orm import Session, sessionmaker

from app.agent.llm import ChatClient, ModelTurn
from app.agent.tools import TOOL_SCHEMAS, ToolError, execute_tool_with_outcome
from app.config import settings
from app.events import append_event
from app.fencing import WorkerFence
from app.telemetry import get_harness_metrics, model_observation, update_model_observation


class BadOutput(RuntimeError):
    pass


SYSTEM_PROMPT = """你是广告投放诊断助手。必须原样保留 campaign_id（包括 camp_ 前缀）。
需要数据时只调用给定工具；拿到足够结果后给出简短中文结论。/no_think"""


def _assistant_message(turn: ModelTurn) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": turn.content}
    if turn.tool_calls:
        message["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments},
            }
            for call in turn.tool_calls
        ]
    return message


def run_agent(
    session_factory: sessionmaker[Session],
    fence: WorkerFence,
    run_id: str,
    prompt: str,
    client: ChatClient,
    *,
    max_rounds: int = 6,
    pause_after_tool_seconds: float = 0,
) -> dict[str, Any]:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]

    for step in range(1, max_rounds + 1):
        with session_factory.begin() as session:
            append_event(
                session,
                run_id,
                "step.model_call",
                {"step": step},
                fence=fence,
            )
        tracer = trace.get_tracer("harness-lab.agent")
        try:
            with model_observation(
                run_id,
                step,
                messages,
                getattr(client, "model", settings.llm_model),
            ) as observation:
                turn = client.complete(messages, TOOL_SCHEMAS)
                update_model_observation(
                    observation,
                    output={
                        "content": turn.content,
                        "tool_calls": [
                            {"id": call.id, "name": call.name, "arguments": call.arguments}
                            for call in turn.tool_calls
                        ],
                    },
                    usage=turn.usage,
                )
        except RateLimitError:
            get_harness_metrics().record_model_call("429")
            raise
        except APITimeoutError:
            get_harness_metrics().record_model_call("timeout")
            raise
        except Exception:
            get_harness_metrics().record_model_call("error")
            raise
        else:
            get_harness_metrics().record_model_call("200")
        messages.append(_assistant_message(turn))

        if turn.tool_calls:
            for call in turn.tool_calls:
                with session_factory.begin() as session:
                    append_event(
                        session,
                        run_id,
                        "step.tool_call",
                        {"step": step, "tool_name": call.name},
                        fence=fence,
                    )
                try:
                    arguments = json.loads(call.arguments)
                    if not isinstance(arguments, dict):
                        raise ValueError("arguments must be an object")
                except (json.JSONDecodeError, ValueError) as exc:
                    raise BadOutput("tool arguments are not a JSON object") from exc
                try:
                    with tracer.start_as_current_span(
                        "harness.tool_call",
                        attributes={"run.id": run_id, "agent.step": step, "tool.name": call.name},
                    ):
                        with session_factory.begin() as session:
                            outcome = execute_tool_with_outcome(
                                session,
                                fence,
                                run_id,
                                step,
                                call.name,
                                arguments,
                            )
                            append_event(
                                session,
                                run_id,
                                "step.tool_result",
                                {"step": step, "tool_name": call.name, "result": outcome.result},
                                fence=fence,
                            )
                    get_harness_metrics().record_tool_latency(
                        call.name,
                        outcome.latency_ms or 0,
                        executed_now=outcome.executed_now,
                    )
                    result = outcome.result
                except ToolError:
                    raise
                if (
                    call.name == "adjust_budget"
                    and fence.attempt == 1
                    and pause_after_tool_seconds > 0
                ):
                    time.sleep(pause_after_tool_seconds)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": json.dumps(result, ensure_ascii=False, separators=(",", ":")),
                    }
                )
            continue

        if turn.content and turn.content.strip():
            return {"answer": turn.content.strip()}
        raise BadOutput("model returned neither tool calls nor final text")

    raise BadOutput(f"model did not finish within {max_rounds} rounds")
