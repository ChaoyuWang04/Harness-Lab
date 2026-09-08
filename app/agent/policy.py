from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal


@dataclass(frozen=True, slots=True)
class BudgetAdjustmentIntent:
    campaign_id: str
    operation: Literal["delta", "percent", "zero", "double"]
    value: Decimal | None = None

    def resolve(self, current_budget: Decimal) -> Decimal:
        if self.operation == "delta":
            assert self.value is not None
            return self.value
        if self.operation == "percent":
            assert self.value is not None
            return current_budget * self.value / Decimal("100")
        if self.operation == "zero":
            return -current_budget
        if self.operation == "double":
            return current_budget
        raise AssertionError(f"unsupported budget operation: {self.operation}")


@dataclass(frozen=True, slots=True)
class ToolPolicy:
    budget_adjustment: BudgetAdjustmentIntent | None


def normalize_authorized_tool_arguments(
    tool_name: str,
    arguments: dict[str, Any],
    policy: ToolPolicy | None,
) -> dict[str, Any]:
    normalized = dict(arguments)
    intent = policy.budget_adjustment if policy is not None else None
    if tool_name != "adjust_budget" or intent is None:
        return normalized
    abbreviated = intent.campaign_id.removeprefix("camp_")
    if str(normalized.get("campaign_id", "")) == abbreviated:
        normalized["campaign_id"] = intent.campaign_id
    return normalized


def _decimal(value: str) -> Decimal:
    return Decimal(value.replace(",", ""))


def build_tool_policy(prompt: str) -> ToolPolicy:
    """Build the single budget-write authorization carried by one agent run.

    The lab intentionally accepts only the small Chinese command grammar used by
    its registered product surface. Unknown or ambiguous wording grants no write
    authorization; read-only tools remain available.
    """

    campaign_ids = set(re.findall(r"\bcamp_\d{3}\b", prompt))
    if len(campaign_ids) != 1:
        return ToolPolicy(budget_adjustment=None)
    campaign_id = next(iter(campaign_ids))

    explicit_delta = re.search(r"调整量为\s*([+-]?\d+(?:\.\d+)?)", prompt)
    if explicit_delta:
        return ToolPolicy(
            BudgetAdjustmentIntent(campaign_id, "delta", _decimal(explicit_delta.group(1)))
        )
    if "清零" in prompt:
        return ToolPolicy(BudgetAdjustmentIntent(campaign_id, "zero"))
    if "翻倍" in prompt:
        return ToolPolicy(BudgetAdjustmentIntent(campaign_id, "double"))

    percent = re.search(r"(增加|提高|减少|降低)\s*(\d+(?:\.\d+)?)\s*%", prompt)
    if percent:
        sign = Decimal("-1") if percent.group(1) in {"减少", "降低"} else Decimal("1")
        return ToolPolicy(
            BudgetAdjustmentIntent(campaign_id, "percent", sign * _decimal(percent.group(2)))
        )

    absolute = re.search(r"(增加|提高|减少|降低)\s*(\d+(?:\.\d+)?)", prompt)
    if absolute:
        sign = Decimal("-1") if absolute.group(1) in {"减少", "降低"} else Decimal("1")
        return ToolPolicy(
            BudgetAdjustmentIntent(campaign_id, "delta", sign * _decimal(absolute.group(2)))
        )

    return ToolPolicy(budget_adjustment=None)
