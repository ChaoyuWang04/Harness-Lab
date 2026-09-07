#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable


def validate_response(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        tool_calls = payload["choices"][0]["message"]["tool_calls"]
        function = tool_calls[0]["function"]
    except (KeyError, IndexError, TypeError):
        return {"valid": False, "reason": "missing_tool_call"}

    if function.get("name") != "get_campaign":
        return {"valid": False, "reason": "wrong_tool_name"}

    try:
        arguments = json.loads(function["arguments"])
    except (KeyError, TypeError, json.JSONDecodeError):
        return {"valid": False, "reason": "arguments_not_json"}

    if arguments.get("campaign_id") != "camp_001":
        return {"valid": False, "reason": "wrong_campaign_id"}

    return {"valid": True, "reason": "valid"}


def build_request(model: str) -> dict[str, Any]:
    return {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": (
                    "当前 camp_001 的预算是多少？调用工具时必须原样保留标识符，"
                    "campaign_id 必须是精确字符串 camp_001，不得省略 camp_ 前缀。 /no_think"
                ),
            }
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "get_campaign",
                    "description": "查询广告计划详情",
                    "parameters": {
                        "type": "object",
                        "properties": {"campaign_id": {"type": "string"}},
                        "required": ["campaign_id"],
                    },
                },
            }
        ],
        "temperature": 0,
        "stream": False,
    }


def build_local_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def sample_responses(
    *,
    base_url: str,
    model: str,
    sample_count: int,
    opener: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    endpoint = f"{base_url.rstrip('/')}/chat/completions"
    samples: list[dict[str, Any]] = []
    valid_count = 0
    open_request = opener or build_local_opener().open

    for sample_index in range(1, sample_count + 1):
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(build_request(model), ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with open_request(request, timeout=120) as response:
                payload = json.loads(response.read())
            classification = validate_response(payload)
            if classification["valid"]:
                valid_count += 1
            samples.append(
                {
                    "sample": sample_index,
                    "classification": classification,
                    "response": payload,
                }
            )
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            samples.append(
                {
                    "sample": sample_index,
                    "classification": {"valid": False, "reason": "request_error"},
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )

    return {
        "base_url": base_url,
        "model": model,
        "sample_count": sample_count,
        "valid_count": valid_count,
        "valid_rate": valid_count / sample_count,
        "samples": samples,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--validate-file", type=Path)
    mode.add_argument("--sample", action="store_true")
    parser.add_argument("--base-url", default="http://127.0.0.1:11434/v1")
    parser.add_argument("--model", default="qwen3:0.6b")
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    if args.validate_file is not None:
        payload = json.loads(args.validate_file.read_text(encoding="utf-8"))
        result = validate_response(payload)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if result["valid"] else 1

    if args.samples <= 0:
        parser.error("--samples must be positive")
    if args.output is None:
        parser.error("--output is required with --sample")

    summary = sample_responses(
        base_url=args.base_url,
        model=args.model,
        sample_count=args.samples,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "model": summary["model"],
                "sample_count": summary["sample_count"],
                "valid_count": summary["valid_count"],
                "valid_rate": summary["valid_rate"],
                "output": str(args.output),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if summary["valid_rate"] >= 0.70 else 1


if __name__ == "__main__":
    raise SystemExit(main())
