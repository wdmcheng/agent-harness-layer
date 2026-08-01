"""共享 parent budget 的原子 claim、settlement 与 terminal guard。"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any, cast

from agent_harness.storage.model_route_chain_state import ModelRouteChainState
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
    """以固定 JSON 编码计算冻结快照摘要，用于检测持久化内容漂移。"""

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
    """将快照中的金额解析为有限非负 Decimal，拒绝布尔值和非数值输入。"""

    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("boolean is not a budget amount")
    parsed = Decimal(str(value))
    if not parsed.is_finite() or parsed < 0:
        raise ValueError("budget amount must be finite and non-negative")
    return parsed


def _non_empty_string(value: object) -> bool:
    """判断快照字段是否为非空字符串，避免 ``str(value)`` 掩盖类型错误。"""

    return isinstance(value, str) and bool(value)


def _snapshot_route_valid(
    route: object,
    *,
    allowed_model_routes: set[tuple[str, str]],
    schema_version: str,
    chain_mode: bool = False,
) -> bool:
    """验证单条冻结路由的字段、价格和模型授权范围是否完整一致。"""

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
        or "input_token_price_usd" not in typed
        or (usage_kind == "model" and "output_token_price_usd" not in typed)
    ):
        return False
    if usage_kind == "embedding" or schema_version == "budget-tree-v1":
        # v1 与 embedding 没有显式 cost-enabled 位，既有来源 identity 仍为必填。
        if not _non_empty_string(typed.get("price_source_ref")) or not _non_empty_string(
            typed.get("price_source_version")
        ):
            return False
    if usage_kind == "model":
        route_key = (
            (cast(str, typed.get("deployment_id")), cast(str, model))
            if chain_mode
            else (cast(str, provider), cast(str, model))
        )
        if route_key not in allowed_model_routes:
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
        if schema_version == "budget-tree-v2":
            required_text = (
                "deployment_id",
                "canonical_base_url",
                "endpoint_origin",
                "endpoint_policy_ref",
                "endpoint_policy_version",
                "endpoint_policy_digest",
                "credential_ref",
                "model_catalog_ref",
                "model_catalog_version",
                "model_catalog_digest",
                "request_shape_ref",
                "request_shape_version",
                "input_bound_strategy_ref",
                "input_bound_strategy_version",
            )
            if not all(_non_empty_string(typed.get(field)) for field in required_text):
                return False
            classifier_ref = typed.get("completion_classifier_ref")
            classifier_version = typed.get("completion_classifier_version")
            if (classifier_ref is None) != (classifier_version is None) or (
                classifier_ref is not None
                and (
                    not _non_empty_string(classifier_ref)
                    or not _non_empty_string(classifier_version)
                )
            ):
                return False
            capabilities = typed.get("capabilities")
            retry_policy = typed.get("retry_policy")
            bulkhead_policy = typed.get("bulkhead_policy")
            positive_int_fields = (
                "max_prompt_utf8_bytes",
                "max_output_tokens",
                "max_per_attempt_token_bound",
                "max_attempts",
                "connect_timeout_ms",
                "read_timeout_ms",
                "total_timeout_ms",
            )
            if (
                not isinstance(capabilities, list)
                or not capabilities
                or any(not _non_empty_string(item) for item in cast(list[object], capabilities))
                or any(
                    item not in {"text_completion", "text_stream"}
                    for item in cast(list[object], capabilities)
                )
                or not isinstance(retry_policy, dict)
                or not isinstance(bulkhead_policy, dict)
                or any(
                    isinstance(typed.get(field), bool)
                    or not isinstance(typed.get(field), int)
                    or cast(int, typed.get(field)) <= 0
                    for field in positive_int_fields
                )
                or isinstance(typed.get("input_envelope_token_bound"), bool)
                or not isinstance(typed.get("input_envelope_token_bound"), int)
                or cast(int, typed.get("input_envelope_token_bound")) < 0
            ):
                return False
            retry = cast(dict[str, object], retry_policy)
            bulkhead = cast(dict[str, object], bulkhead_policy)
            max_prompt = cast(int, typed.get("max_prompt_utf8_bytes"))
            envelope = cast(int, typed.get("input_envelope_token_bound"))
            max_output = cast(int, typed.get("max_output_tokens"))
            if typed.get("max_per_attempt_token_bound") != max_prompt + envelope + max_output:
                return False
            retry_statuses = retry.get("retryable_http_statuses")
            failover_statuses = typed.get("cross_provider_failover_http_statuses")
            if (
                not isinstance(retry_statuses, list)
                or any(
                    isinstance(item, bool) or not isinstance(item, int) or item < 100 or item > 599
                    for item in cast(list[object], retry_statuses)
                )
                or retry.get("max_attempts") != typed.get("max_attempts")
                or bulkhead.get("scope") != "process_deployment"
                or not isinstance(failover_statuses, list)
                or any(
                    isinstance(item, bool) or not isinstance(item, int) or item < 400 or item > 599
                    for item in cast(list[object], failover_statuses)
                )
            ):
                return False
            if retry_statuses and (
                classifier_ref != "trusted_response_header_not_started"
                or classifier_version != "v1"
            ):
                return False
            cost_enabled = typed.get("cost_enabled")
            if not isinstance(cost_enabled, bool):
                return False
            price_values = (
                typed.get("input_token_price_usd"),
                typed.get("output_token_price_usd"),
                typed.get("price_source_ref"),
                typed.get("price_source_version"),
                typed.get("max_per_attempt_cost_bound"),
            )
            if cost_enabled:
                if (
                    not _non_empty_string(typed.get("price_source_ref"))
                    or not _non_empty_string(typed.get("price_source_version"))
                    or any(item is None for item in price_values[:2])
                    or typed.get("max_per_attempt_cost_bound") is None
                ):
                    return False
                try:
                    input_price = _snapshot_decimal(typed.get("input_token_price_usd"))
                    output_price = _snapshot_decimal(typed.get("output_token_price_usd"))
                    maximum_cost = _snapshot_decimal(typed.get("max_per_attempt_cost_bound"))
                except (ArithmeticError, ValueError):
                    return False
                if (
                    input_price is None
                    or output_price is None
                    or maximum_cost
                    != Decimal(max_prompt + envelope) * input_price
                    + Decimal(max_output) * output_price
                ):
                    return False
            elif any(item is not None for item in price_values):
                return False
    return True


def _agent_sub_snapshot_valid(
    value: object,
    *,
    agent_id: str,
    owner_token_limit: int,
    owner_cost_limit: Decimal | None,
    schema_version: str,
) -> bool:
    """验证单个 agent 的子快照不突破 owner 预算且覆盖其允许模型路由。"""

    if not isinstance(value, dict):
        return False
    typed = cast(dict[str, object], value)
    model_policy = typed.get("model_policy")
    target_budget = typed.get("target_budget")
    routes = typed.get("routes")
    # agent 身份、模型策略、目标预算和路线必须一起出现；部分对象视为无效快照。
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
    chain_mode = False
    if schema_version == "budget-tree-v2":
        deployment_id = policy.get("deployment_id")
        raw_allowed = policy.get("allowed_models")
        allowed_values = cast(list[object], raw_allowed) if isinstance(raw_allowed, list) else []
        if (
            not _non_empty_string(deployment_id)
            or not isinstance(raw_allowed, list)
            or not all(_non_empty_string(item) for item in allowed_values)
            or len(allowed_values) != len(set(cast(list[str], allowed_values)))
            or not set(policy_models) <= set(cast(list[str], allowed_values))
        ):
            return False
        raw_route_refs = policy.get("fallback_routes")
        if raw_route_refs:
            if (
                not isinstance(raw_route_refs, list)
                or not 1 <= len(cast(list[object], raw_route_refs)) <= 8
            ):
                return False
            route_refs = cast(list[object], raw_route_refs)
            if any(
                not isinstance(item, dict)
                or not _non_empty_string(cast(dict[str, object], item).get("deployment_id"))
                or not _non_empty_string(cast(dict[str, object], item).get("model_id"))
                for item in route_refs
            ):
                return False
            allowed_model_routes = {
                (
                    cast(str, cast(dict[str, object], item).get("deployment_id")),
                    cast(str, cast(dict[str, object], item).get("model_id")),
                )
                for item in route_refs
            }
            if len(allowed_model_routes) != len(route_refs):
                return False
            chain_mode = True
        else:
            allowed_model_routes = {
                (cast(str, provider), model) for model in cast(list[str], allowed_values)
            }
    elif provider != "fake":
        # v1 从未冻结真实 deployment/catalog/endpoint identity，禁止用当前配置补齐。
        return False
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
            schema_version=schema_version,
            chain_mode=chain_mode,
        )
        for route in route_values
    ):
        return False
    frozen_model_routes = [
        (
            cast(
                str,
                cast(dict[str, object], route).get("deployment_id" if chain_mode else "provider"),
            ),
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
    if data.snapshot_id.startswith("budget-tree-v2:"):
        schema_version = "budget-tree-v2"
    elif data.snapshot_id.startswith(("budget-tree-v1:", "snapshot:")):
        # ``snapshot:`` 是已持久化的完整 v1 fixture/历史记录 identity；它仍只允许 fake。
        schema_version = "budget-tree-v1"
    else:
        return False
    if data.snapshot.get("schema_version", schema_version) != schema_version:
        return False
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
                schema_version=schema_version,
            )
            for agent_id in {root.agent_id, *targets}
        )
    )


def _decimal(value: Decimal | None) -> Decimal:
    """将可选账本影响统一为可安全累加的零值 Decimal。"""

    return _ZERO if value is None else value


def _ledger_record(model: ParentBudgetLedgerModel) -> LedgerRecord:
    """将账本 ORM 模型映射为领域记录，不暴露冻结快照原文。"""

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
    """将直接预算 claim 映射为领域记录，并显式标记本次是否来自重放。"""

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
        route_chain_state=(
            None
            if model.route_chain_state_json is None
            else ModelRouteChainState.model_validate(model.route_chain_state_json)
        ),
        replayed=replayed,
    )


def _allocation_record(
    model: DelegationBudgetAllocationModel, *, replayed: bool = False
) -> AllocationRecord:
    """将委派预算分配映射为领域记录，保留副作用状态供恢复逻辑判断。"""

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
        route_chain_state=(
            None
            if model.route_chain_state_json is None
            else ModelRouteChainState.model_validate(model.route_chain_state_json)
        ),
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
