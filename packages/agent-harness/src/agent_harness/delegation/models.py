"""受控 delegation 的稳定请求、结果与可信聚合 DTO。"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from agent_harness.contracts.dto import HarnessDTO
from agent_harness.identity import IdentityContext

DelegationBudgetStatus = Literal["within_budget", "exceeded", "incomplete"]
DelegationCostStatus = Literal["reported", "estimated", "unavailable"]


class DelegationRequest(HarnessDTO):
    """内置 `agent.delegate` seam 接收的稳定业务请求。"""

    parent_run_id: str = Field(min_length=1)
    source_agent_id: str = Field(min_length=1)
    target_agent_id: str = Field(min_length=1)
    child_input: dict[str, Any]
    idempotency_key: str = Field(min_length=1)
    budget_intent: Literal["inherit_parent"] = "inherit_parent"
    request_id: str | None = None


class DelegationChildEvidence(HarnessDTO):
    """从持久化 child run/usage/trace evidence 归一化出的可信输入。"""

    run_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    status: str = Field(min_length=1)
    input_tokens: int | None
    output_tokens: int | None
    input_tokens_complete: bool = True
    output_tokens_complete: bool = True
    cost_usd: float | None
    cost_status: DelegationCostStatus
    latency_ms: int | None
    usage_evidence_refs: list[str] = Field(default_factory=list)
    trace_refs: list[str] = Field(default_factory=list)

    @field_validator("input_tokens", "output_tokens", "latency_ms", mode="before")
    @classmethod
    def validate_optional_integer(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("delegation integer evidence must be non-negative or null")
        return value

    @field_validator("cost_usd", mode="before")
    @classmethod
    def validate_optional_cost(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError("delegation cost evidence must be numeric or null")
        if not math.isfinite(value) or value < 0:
            raise ValueError("delegation cost evidence must be finite and non-negative")
        return value

    @model_validator(mode="after")
    def validate_cost_status(self) -> DelegationChildEvidence:
        if self.cost_status == "unavailable":
            if self.cost_usd is not None:
                raise ValueError("unavailable delegation cost requires null value")
        elif self.cost_usd is None:
            raise ValueError("reported or estimated delegation cost requires a value")
        return self


class DelegationChildSummary(HarnessDTO):
    """公开 parent detail 中允许暴露的 child 引用。"""

    run_id: str
    agent_id: str
    status: Literal["created", "running", "waiting", "completed", "failed", "cancelled"]
    usage_evidence_refs: list[str]
    trace_refs: list[str]


class DelegationSummary(HarnessDTO):
    """API Contract 5.30 的 durable parent aggregation。"""

    parent_run_id: str
    children: list[DelegationChildSummary]
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: int | None
    cost_usd: float | None
    budget_status: DelegationBudgetStatus
    trace_refs: list[str]


def delegation_request_hash(
    request: DelegationRequest,
    *,
    identity: IdentityContext,
) -> str:
    """绑定稳定安全上下文；动态余额和锁内 reservation 不参与 hash。"""

    payload = {
        "tenant_id": identity.tenant_id,
        "identity": {
            "user_id": identity.user_id,
            "session_id": identity.session_id,
            "roles": sorted(identity.roles),
            "permissions": sorted(identity.permissions),
            "auth_method": identity.auth_method,
        },
        "parent_run_id": request.parent_run_id,
        "source_agent_id": request.source_agent_id,
        "target_agent_id": request.target_agent_id,
        "child_input": request.child_input,
        "budget_intent": request.budget_intent,
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def aggregate_delegation_evidence(
    *,
    parent_run_id: str,
    children: list[DelegationChildEvidence],
    budget_exceeded: bool = False,
) -> DelegationSummary:
    """只聚合已通过 DTO 校验的 durable evidence，不把 unknown 当作零。"""

    input_tokens = _known_integer_sum([child.input_tokens for child in children])
    output_tokens = _known_integer_sum([child.output_tokens for child in children])
    complete_tokens = all(
        child.input_tokens is not None
        and child.output_tokens is not None
        and child.input_tokens_complete
        and child.output_tokens_complete
        for child in children
    )
    complete_cost = all(child.cost_status != "unavailable" for child in children)
    cost_usd = sum(child.cost_usd or 0 for child in children) if complete_cost else None
    complete_latency = all(child.latency_ms is not None for child in children)
    latency_ms = sum(child.latency_ms or 0 for child in children) if complete_latency else None
    complete = complete_tokens and complete_cost and complete_latency
    # 任一 token/cost/latency 不完整时，`exceeded` 只代表局部已知值，不能覆盖
    # parent 总量未知的事实；完整性必须优先于预算比较结果。
    budget_status: DelegationBudgetStatus = (
        "incomplete" if not complete else "exceeded" if budget_exceeded else "within_budget"
    )
    trace_refs = list(dict.fromkeys(ref for child in children for ref in child.trace_refs))
    return DelegationSummary(
        parent_run_id=parent_run_id,
        children=[
            DelegationChildSummary.model_validate(
                {
                    "run_id": child.run_id,
                    "agent_id": child.agent_id,
                    "status": child.status,
                    "usage_evidence_refs": child.usage_evidence_refs,
                    "trace_refs": child.trace_refs,
                }
            )
            for child in children
        ],
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        latency_ms=latency_ms,
        cost_usd=cost_usd,
        budget_status=budget_status,
        trace_refs=trace_refs,
    )


def _known_integer_sum(values: list[int | None]) -> int | None:
    known = [value for value in values if value is not None]
    return sum(known) if known else None


__all__ = [
    "DelegationBudgetStatus",
    "DelegationChildEvidence",
    "DelegationChildSummary",
    "DelegationCostStatus",
    "DelegationRequest",
    "DelegationSummary",
    "aggregate_delegation_evidence",
    "delegation_request_hash",
]
