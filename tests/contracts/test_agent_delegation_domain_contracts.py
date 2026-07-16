"""Delegation 的稳定请求与聚合领域合同。"""

from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from agent_harness.delegation import (
    DelegationChildEvidence,
    DelegationCostStatus,
    DelegationRequest,
    aggregate_delegation_evidence,
    delegation_request_hash,
)
from agent_harness.identity import IdentityContext


def _identity(*, tenant_id: str = "tenant-a", user_id: str = "user-a") -> IdentityContext:
    return IdentityContext(
        tenant_id=tenant_id,
        user_id=user_id,
        session_id="session-a",
        roles=["operator"],
        permissions=["agent.delegate"],
        auth_method="api-key",
    )


def _request(**updates: object) -> DelegationRequest:
    payload: dict[str, object] = {
        "parent_run_id": "run-parent",
        "source_agent_id": "agent-source",
        "target_agent_id": "agent-target",
        "child_input": {"query": "safe", "options": {"limit": 3}},
        "idempotency_key": "delegation-key",
    }
    payload.update(updates)
    return DelegationRequest.model_validate(payload)


def test_request_hash_is_stable_and_binds_security_context() -> None:
    """字典顺序不影响 hash，tenant/identity/目标/input 任一变化都必须改变。"""

    first = _request()
    reordered = _request(child_input={"options": {"limit": 3}, "query": "safe"})
    baseline = delegation_request_hash(first, identity=_identity())

    assert first.budget_intent == "inherit_parent"
    assert delegation_request_hash(reordered, identity=_identity()) == baseline
    assert delegation_request_hash(first, identity=_identity(tenant_id="tenant-b")) != baseline
    assert delegation_request_hash(first, identity=_identity(user_id="user-b")) != baseline
    assert (
        delegation_request_hash(_request(target_agent_id="agent-other"), identity=_identity())
        != baseline
    )
    assert (
        delegation_request_hash(_request(child_input={"query": "changed"}), identity=_identity())
        != baseline
    )


def test_mixed_unknown_evidence_preserves_known_tokens_without_faking_totals() -> None:
    summary = aggregate_delegation_evidence(
        parent_run_id="run-parent",
        children=[
            DelegationChildEvidence(
                run_id="run-child-a",
                agent_id="agent-target",
                status="completed",
                input_tokens=7,
                output_tokens=3,
                cost_usd=0.25,
                cost_status="reported",
                latency_ms=12,
                usage_evidence_refs=["usage:a"],
                trace_refs=["trace-a"],
            ),
            DelegationChildEvidence(
                run_id="run-child-b",
                agent_id="agent-target",
                status="completed",
                input_tokens=None,
                output_tokens=2,
                cost_usd=None,
                cost_status="unavailable",
                latency_ms=None,
                usage_evidence_refs=["usage:b"],
                trace_refs=["trace-a", "trace-b"],
            ),
        ],
    )

    assert summary.input_tokens == 7
    assert summary.output_tokens == 5
    assert summary.cost_usd is None
    assert summary.latency_ms is None
    assert summary.budget_status == "incomplete"
    assert summary.trace_refs == ["trace-a", "trace-b"]


def test_all_unknown_tokens_remain_null() -> None:
    summary = aggregate_delegation_evidence(
        parent_run_id="run-parent",
        children=[
            DelegationChildEvidence(
                run_id="run-child",
                agent_id="agent-target",
                status="failed",
                input_tokens=None,
                output_tokens=None,
                cost_usd=None,
                cost_status="unavailable",
                latency_ms=None,
                usage_evidence_refs=[],
                trace_refs=["trace-child"],
            )
        ],
    )

    assert summary.input_tokens is None
    assert summary.output_tokens is None
    assert summary.cost_usd is None
    assert summary.latency_ms is None
    assert summary.budget_status == "incomplete"


def test_incomplete_evidence_takes_precedence_over_known_budget_excess() -> None:
    summary = aggregate_delegation_evidence(
        parent_run_id="run-parent",
        budget_exceeded=True,
        children=[
            DelegationChildEvidence(
                run_id="run-child-complete",
                agent_id="agent-target",
                status="completed",
                input_tokens=8,
                output_tokens=7,
                cost_usd=1.0,
                cost_status="reported",
                latency_ms=5,
                usage_evidence_refs=["usage:complete"],
                trace_refs=["trace-complete"],
            ),
            DelegationChildEvidence(
                run_id="run-child-incomplete",
                agent_id="agent-target",
                status="completed",
                input_tokens=None,
                output_tokens=2,
                cost_usd=None,
                cost_status="unavailable",
                latency_ms=None,
                usage_evidence_refs=["usage:incomplete"],
                trace_refs=["trace-incomplete"],
            ),
        ],
    )

    assert summary.input_tokens == 8
    assert summary.output_tokens == 9
    assert summary.cost_usd is None
    assert summary.latency_ms is None
    assert summary.budget_status == "incomplete"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("input_tokens", True),
        ("output_tokens", -1),
        ("cost_usd", -0.1),
        ("cost_usd", math.nan),
        ("cost_usd", math.inf),
        ("latency_ms", -1),
    ],
)
def test_child_evidence_rejects_invalid_numeric_values(field: str, value: object) -> None:
    payload: dict[str, object] = {
        "run_id": "run-child",
        "agent_id": "agent-target",
        "status": "completed",
        "input_tokens": 1,
        "output_tokens": 1,
        "cost_usd": 0.1,
        "cost_status": "reported",
        "latency_ms": 1,
        "usage_evidence_refs": ["usage:child"],
        "trace_refs": ["trace-child"],
    }
    payload[field] = value

    with pytest.raises(ValidationError):
        DelegationChildEvidence.model_validate(payload)


@pytest.mark.parametrize(
    ("cost_usd", "cost_status"),
    [(None, "reported"), (0.1, "unavailable")],
)
def test_child_evidence_rejects_cost_status_mismatch(
    cost_usd: float | None,
    cost_status: DelegationCostStatus,
) -> None:
    with pytest.raises(ValidationError):
        DelegationChildEvidence(
            run_id="run-child",
            agent_id="agent-target",
            status="completed",
            input_tokens=1,
            output_tokens=1,
            cost_usd=cost_usd,
            cost_status=cost_status,
            latency_ms=1,
            usage_evidence_refs=["usage:child"],
            trace_refs=["trace-child"],
        )
