"""共享 parent budget 的原子 claim、settlement 与 terminal guard。"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any, cast

from agent_harness.storage.run_models import AgentRunModel
from agent_harness.storage.shared_budget import (
    AllocationRecord,
    ClaimRecord,
    LedgerCreate,
    LedgerRecord,
)
from agent_harness.storage.shared_budget_models import (
    BudgetOperationClaimModel,
    DelegationBudgetAllocationModel,
    ParentBudgetLedgerModel,
)

_ZERO = Decimal("0")


def _snapshot_hash(snapshot: object) -> str:
    encoded = json.dumps(
        snapshot,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _ledger_snapshot_valid(model: ParentBudgetLedgerModel) -> bool:
    """所有读写 seam 都先验证 frozen catalog 未被数据库外部改写。"""

    return _snapshot_hash(model.snapshot_json) == model.snapshot_hash


def _snapshot_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("boolean is not a budget amount")
    parsed = Decimal(str(value))
    if not parsed.is_finite() or parsed < 0:
        raise ValueError("budget amount must be finite and non-negative")
    return parsed


def _non_empty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def _snapshot_route_valid(
    route: object,
    *,
    allowed_model_routes: set[tuple[str, str]],
) -> bool:
    if not isinstance(route, dict):
        return False
    typed = cast(dict[str, object], route)
    usage_kind = typed.get("usage_kind")
    provider = typed.get("provider")
    model = typed.get("model")
    if (
        usage_kind not in {"model", "embedding"}
        or not _non_empty_string(provider)
        or not _non_empty_string(model)
        or not _non_empty_string(typed.get("price_source_ref"))
        or not _non_empty_string(typed.get("price_source_version"))
        or "input_token_price_usd" not in typed
        or (usage_kind == "model" and "output_token_price_usd" not in typed)
    ):
        return False
    if (
        usage_kind == "model"
        and (cast(str, provider), cast(str, model)) not in allowed_model_routes
    ):
        return False
    try:
        _snapshot_decimal(typed.get("input_token_price_usd"))
        if usage_kind == "model":
            _snapshot_decimal(typed.get("output_token_price_usd"))
    except (ArithmeticError, ValueError):
        return False
    if usage_kind == "model":
        soft_limit = typed.get("soft_max_tokens_per_call")
        if isinstance(soft_limit, bool) or not isinstance(soft_limit, int) or soft_limit < 0:
            return False
    return True


def _agent_sub_snapshot_valid(
    value: object,
    *,
    agent_id: str,
    owner_token_limit: int,
    owner_cost_limit: Decimal | None,
) -> bool:
    if not isinstance(value, dict):
        return False
    typed = cast(dict[str, object], value)
    model_policy = typed.get("model_policy")
    target_budget = typed.get("target_budget")
    routes = typed.get("routes")
    if (
        typed.get("agent_id") != agent_id
        or not _non_empty_string(typed.get("descriptor_version"))
        or not isinstance(model_policy, dict)
        or not isinstance(target_budget, dict)
        or not isinstance(routes, list)
        or not routes
    ):
        return False
    policy = cast(dict[str, object], model_policy)
    provider = policy.get("provider")
    default_model = policy.get("default_model")
    raw_fallbacks = policy.get("fallback_models")
    if (
        not _non_empty_string(provider)
        or not _non_empty_string(default_model)
        or not isinstance(raw_fallbacks, list)
    ):
        return False
    fallback_values = cast(list[object], raw_fallbacks)
    if not all(_non_empty_string(item) for item in fallback_values):
        return False
    policy_models = [cast(str, default_model), *cast(list[str], fallback_values)]
    if len(set(policy_models)) != len(policy_models):
        return False
    allowed_model_routes = {(cast(str, provider), model) for model in policy_models}
    budget = cast(dict[str, object], target_budget)
    token_limit = budget.get("max_tokens_per_run")
    if (
        isinstance(token_limit, bool)
        or not isinstance(token_limit, int)
        or token_limit < 0
        or token_limit > owner_token_limit
    ):
        return False
    try:
        cost_limit = _snapshot_decimal(budget.get("max_cost_usd_per_run"))
    except (ArithmeticError, ValueError):
        return False
    if (owner_cost_limit is None and cost_limit is not None) or (
        owner_cost_limit is not None and cost_limit is not None and cost_limit > owner_cost_limit
    ):
        return False
    route_values = cast(list[object], routes)
    if not all(
        _snapshot_route_valid(
            route,
            allowed_model_routes=allowed_model_routes,
        )
        for route in route_values
    ):
        return False
    frozen_model_routes = [
        (
            cast(str, cast(dict[str, object], route).get("provider")),
            cast(str, cast(dict[str, object], route).get("model")),
        )
        for route in route_values
        if isinstance(route, dict) and cast(dict[str, object], route).get("usage_kind") == "model"
    ]
    return (
        len(frozen_model_routes) == len(allowed_model_routes)
        and set(frozen_model_routes) == allowed_model_routes
    )


def _ledger_create_snapshot_valid(data: LedgerCreate, root: AgentRunModel) -> bool:
    """在首次写入前逐值验证 owner envelope，禁止从 agents 目录推断授权边。"""

    raw_owner = data.snapshot.get("owner")
    raw_agents = data.snapshot.get("agents")
    if not isinstance(raw_owner, dict) or not isinstance(raw_agents, dict):
        return False
    owner = cast(dict[str, object], raw_owner)
    agents = cast(dict[str, object], raw_agents)
    raw_targets = owner.get("delegation_targets")
    if not isinstance(raw_targets, list):
        return False
    target_values = cast(list[object], raw_targets)
    targets = [item for item in target_values if isinstance(item, str) and item]
    if (
        len(targets) != len(target_values)
        or len(set(targets)) != len(targets)
        or root.agent_id not in agents
    ):
        return False
    try:
        owner_cost = _snapshot_decimal(owner.get("max_cost_usd_per_run"))
    except (ArithmeticError, ValueError):
        return False
    return (
        owner.get("agent_id") == root.agent_id
        and owner.get("root_run_id") == data.budget_owner_run_id
        and owner.get("max_tokens_per_run") == data.token_limit
        and owner_cost == data.cost_limit
        and owner.get("cost_enabled") is (data.cost_limit is not None)
        and data.snapshot.get("registry_version") == data.registry_version
        and data.snapshot.get("config_version") == data.config_version
        and data.snapshot.get("catalog_version") == data.catalog_version
        and all(
            _agent_sub_snapshot_valid(
                agents.get(agent_id),
                agent_id=agent_id,
                owner_token_limit=data.token_limit,
                owner_cost_limit=data.cost_limit,
            )
            for agent_id in {root.agent_id, *targets}
        )
    )


def _decimal(value: Decimal | None) -> Decimal:
    return _ZERO if value is None else value


def _ledger_record(model: ParentBudgetLedgerModel) -> LedgerRecord:
    return LedgerRecord(
        tenant_id=model.tenant_id,
        budget_owner_run_id=model.budget_owner_run_id,
        token_limit=model.token_limit,
        cost_limit=model.cost_limit,
        token_impact=model.token_impact,
        cost_impact=model.cost_impact,
        state=cast(Any, model.state),
        version=model.version,
        snapshot_id=model.snapshot_id,
    )


def _claim_record(model: BudgetOperationClaimModel, *, replayed: bool = False) -> ClaimRecord:
    return ClaimRecord(
        id=model.id,
        tenant_id=model.tenant_id,
        budget_owner_run_id=model.budget_owner_run_id,
        operation_kind=cast(Any, model.operation_kind),
        usage_call_id=model.usage_call_id,
        delegation_id=model.delegation_id,
        state=cast(Any, model.state),
        side_effect_state=cast(Any, model.side_effect_state),
        token_impact=model.token_impact,
        cost_impact=model.cost_impact,
        result=model.result_json,
        replayed=replayed,
    )


def _allocation_record(
    model: DelegationBudgetAllocationModel, *, replayed: bool = False
) -> AllocationRecord:
    return AllocationRecord(
        id=model.id,
        tenant_id=model.tenant_id,
        budget_owner_run_id=model.budget_owner_run_id,
        delegation_id=model.delegation_id,
        usage_call_id=model.usage_call_id,
        state=cast(Any, model.state),
        side_effect_state=cast(Any, model.side_effect_state),
        token_impact=model.token_impact,
        cost_impact=model.cost_impact,
        result=model.result_json,
        replayed=replayed,
    )


__all__ = [
    "_agent_sub_snapshot_valid",
    "_allocation_record",
    "_claim_record",
    "_decimal",
    "_ledger_create_snapshot_valid",
    "_ledger_record",
    "_ledger_snapshot_valid",
    "_non_empty_string",
    "_snapshot_decimal",
    "_snapshot_hash",
    "_snapshot_route_valid",
]
