"""DelegationService 合同的 descriptor、identity 与 policy 共享夹具。"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any, cast

from tests.contracts.provider_neutral_structured_output_test_support import (
    fixture_output_schema_identity,
)

from agent_harness.contracts import GuardrailDecisionStatus
from agent_harness.delegation.models import (
    DelegationRequest,
    delegation_relation_id,
    delegation_request_bytes,
    delegation_request_hash,
)
from agent_harness.identity import IdentityContext
from agent_harness.policy import PolicyCheck, PolicyEvaluation
from agent_harness.registry import (
    AgentBudget,
    AgentDescriptor,
    AgentModelPolicy,
    AgentToolPolicy,
)
from agent_harness.runtime.shared_budget import SharedBudgetRuntime
from agent_harness.storage.delegation_repositories import DelegationClaimCreate
from agent_harness.storage.shared_budget import OperationIdentity


def _descriptor(
    agent_id: str,
    *,
    targets: list[str],
    max_tokens: int = 100,
    max_cost_usd: float | None = 10.0,
) -> AgentDescriptor:
    """构造可 delegation 的最小 agent descriptor，控制目标和预算以复用授权场景。"""

    return AgentDescriptor(
        agent_id=agent_id,
        version="1",
        name=agent_id,
        description="delegation contract agent",
        input_schema_ref="schemas/input.json",
        output_schema_ref="schemas/output.json",
        output_schema_identity=fixture_output_schema_identity(
            schema_ref="schemas/output.json",
            version="1",
        ),
        config_ref=f"agents/{agent_id}/config.yaml",
        tool_policy=AgentToolPolicy(allowed_tools=["agent.delegate"]),
        model_policy=AgentModelPolicy(
            provider="fake",
            default_model="fake-basic",
            fallback_models=[],
        ),
        budget=AgentBudget(
            max_tokens_per_run=max_tokens,
            max_cost_usd_per_run=max_cost_usd,
        ),
        eval_dataset=None,
        delegation_targets=targets,
    )


def _identity(*, permissions: list[str] | None = None) -> IdentityContext:
    """构造固定 tenant/session 身份，并允许测试收窄或清空 delegation 权限。"""

    return IdentityContext(
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
        roles=["operator"],
        permissions=permissions if permissions is not None else ["agent.delegate"],
        auth_method="api-key",
    )


class _Policy:
    """仅按 actor 权限允许 delegation 的确定性策略替身。"""

    async def evaluate(self, check: PolicyCheck) -> PolicyEvaluation:
        """将 ``agent.delegate`` 权限映射为 allow/deny，保留完整审计关联字段。"""

        decision = (
            GuardrailDecisionStatus.ALLOW.value
            if "agent.delegate" in check.actor.permissions
            else GuardrailDecisionStatus.DENY.value
        )
        return PolicyEvaluation(
            decision=decision,
            reason="contract policy",
            actor=check.actor,
            action=check.action,
            resource=check.resource,
        )


class _SharedBudgetRuntimeFixture:
    """让 delegation 合同夹具走与真实 composition 相同的 allocation seam。"""

    _fingerprint_key = b"delegation-contract-budget-key"
    _fingerprint_key_version = "delegation-contract-v1"

    def operation_identity(self, **values: Any) -> OperationIdentity:
        """以固定测试 fingerprint 密钥构造通用操作身份，保证重放断言可复现。"""

        return OperationIdentity.from_semantic_request(
            fingerprint_key=self._fingerprint_key,
            fingerprint_key_version=self._fingerprint_key_version,
            **values,
        )

    def delegation_identity(
        self,
        *,
        tenant_id: str,
        canonical_request_bytes: bytes,
        parent_run_id: str,
        source_agent_id: str,
        target_agent_id: str,
        delegation_id: str,
        idempotency_key: str,
        tree_snapshot_id: str,
        snapshot: dict[str, Any],
        trusted_token_bound: int,
        trusted_cost_bound: Decimal | None,
    ) -> OperationIdentity:
        """从冻结目标 routes 重建 delegation 身份，验证目录 hash 与额度绑定。"""

        agents = cast(dict[str, object], snapshot["agents"])
        target = cast(dict[str, object], agents[target_agent_id])
        routes = cast(list[object], target["routes"])
        catalog_digest = hashlib.sha256(
            json.dumps(
                routes,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        return OperationIdentity.from_delegation_request(
            tenant_id=tenant_id,
            fingerprint_key=self._fingerprint_key,
            fingerprint_key_version=self._fingerprint_key_version,
            canonical_request_bytes=canonical_request_bytes,
            parent_run_id=parent_run_id,
            source_agent_id=source_agent_id,
            target_agent_id=target_agent_id,
            delegation_claim_id=delegation_id,
            operation_slot=idempotency_key,
            tree_snapshot_id=tree_snapshot_id,
            target_sub_snapshot_id=f"{tree_snapshot_id}:{target_agent_id}",
            target_route_catalog_digest=f"budget-routes-v1:{catalog_digest}",
            cost_enabled=trusted_cost_bound is not None,
            trusted_token_bound=trusted_token_bound,
            trusted_cost_bound=trusted_cost_bound,
        )

    def delegation_replay_identity(
        self,
        *,
        tenant_id: str,
        canonical_request_bytes: bytes,
        parent_run_id: str,
        source_agent_id: str,
        target_agent_id: str,
        delegation_id: str,
        idempotency_key: str,
        persisted_identity: OperationIdentity,
    ) -> OperationIdentity:
        """基于首次持久化身份重建重放请求，避免从当前配置重新取授权快照。"""

        assert persisted_identity.target_route_catalog_digest is not None
        return OperationIdentity.from_delegation_request(
            tenant_id=tenant_id,
            fingerprint_key=self._fingerprint_key,
            fingerprint_key_version=self._fingerprint_key_version,
            canonical_request_bytes=canonical_request_bytes,
            parent_run_id=parent_run_id,
            source_agent_id=source_agent_id,
            target_agent_id=target_agent_id,
            delegation_claim_id=delegation_id,
            operation_slot=idempotency_key,
            tree_snapshot_id=persisted_identity.tree_snapshot_id,
            target_sub_snapshot_id=persisted_identity.agent_sub_snapshot_id,
            target_route_catalog_digest=persisted_identity.target_route_catalog_digest,
            cost_enabled=persisted_identity.cost_enabled,
            trusted_token_bound=persisted_identity.trusted_token_bound,
            trusted_cost_bound=persisted_identity.trusted_cost_bound,
        )

    model_router_config = SharedBudgetRuntime.model_router_config


def delegation_claim(
    *,
    parent_run_id: str,
    request: DelegationRequest,
    identity: IdentityContext,
    tree_snapshot_id: str,
    snapshot: dict[str, Any],
    token_bound: int = 100,
    cost_bound: Decimal | None = Decimal("10"),
) -> DelegationClaimCreate:
    """构造包含冻结预算身份的 delegation claim DTO，供仓储和恢复合同共享。"""

    delegation_id = delegation_relation_id(
        tenant_id=identity.tenant_id,
        parent_run_id=parent_run_id,
        idempotency_key=request.idempotency_key,
    )
    return DelegationClaimCreate(
        delegation_id=delegation_id,
        tenant_id=identity.tenant_id,
        parent_run_id=parent_run_id,
        source_agent_id=request.source_agent_id,
        target_agent_id=request.target_agent_id,
        idempotency_key=request.idempotency_key,
        request_hash=delegation_request_hash(request, identity=identity),
        budget_intent=request.budget_intent,
        child_input=request.child_input,
        identity=identity.to_payload(),
        trace_id="trace-parent",
        request_id=request.request_id,
        parent_token_limit=100,
        requested_token_reservation=token_bound,
        parent_cost_limit=None if cost_bound is None else 10.0,
        requested_cost_reservation=None if cost_bound is None else float(cost_bound),
        budget_identity=_SharedBudgetRuntimeFixture().delegation_identity(
            tenant_id=identity.tenant_id,
            canonical_request_bytes=delegation_request_bytes(request, identity=identity),
            parent_run_id=parent_run_id,
            source_agent_id=request.source_agent_id,
            target_agent_id=request.target_agent_id,
            delegation_id=delegation_id,
            idempotency_key=request.idempotency_key,
            tree_snapshot_id=tree_snapshot_id,
            snapshot=snapshot,
            trusted_token_bound=token_bound,
            trusted_cost_bound=cost_bound,
        ),
    )


__all__ = [
    "_Policy",
    "_SharedBudgetRuntimeFixture",
    "_descriptor",
    "_identity",
    "delegation_claim",
]
