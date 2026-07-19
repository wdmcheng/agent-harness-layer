"""嵌入耐久用量的身份校验与精确重放，隔离当前缓存与首次执行快照。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from agent_harness.embeddings.provider import EmbeddingRequest
from agent_harness.models.usage import UsageEvidenceContext, UsageInvocationReplayError
from agent_harness.storage.adapters.sqlalchemy import SQLAlchemyStorage
from agent_harness.storage.evidence_repositories import (
    EvidenceOperationKind,
    UsageSettlementClaim,
)
from agent_harness.storage.shared_budget import (
    BudgetOperationOwnership,
    OperationIdentity,
)


@dataclass(frozen=True)
class _EmbeddingSettlement:
    """重放前读取到的用量结算 claim、预算所有权和是否可安全启动标记。"""

    usage: UsageSettlementClaim
    ownership: BudgetOperationOwnership | None
    safe_to_start: bool = False


class _IdentityRuntime(Protocol):
    """重放时构造和验证共享预算身份所需的最小运行时协议。"""

    def operation_identity(self, **values: Any) -> OperationIdentity:
        """按首次快照参数重建期望操作身份，用于检测重放输入漂移。"""
        ...

    def embedding_price_config(
        self,
        *,
        snapshot: dict[str, Any],
        agent_id: str,
        provider: str,
        model: str,
    ) -> tuple[Decimal | None, str, str]:
        """解析嵌入定价配置；协议与模型调用路径保持相同的来源版本形状。"""
        ...


class _EmbeddingReplayMixin:
    """只负责在当前 cache、snapshot 与 provider 之前恢复 durable 结果。"""

    _storage: SQLAlchemyStorage
    _shared_budget: _IdentityRuntime | None

    @staticmethod
    def _final_event_id(tenant_id: str, usage_call_id: str) -> str:
        """由宿主服务提供稳定 final event ID，使 replay 查询同一条用量证据。"""
        ...

    @staticmethod
    def _semantic_request(request: EmbeddingRequest) -> dict[str, object]:
        """提取参与身份指纹的最小语义输入，排除运行时可变字段。"""
        return {"input": request.input}

    async def _replay_settlement_before_current_snapshot(
        self,
        *,
        request: EmbeddingRequest,
        context: UsageEvidenceContext,
        usage_call_id: str,
    ) -> _EmbeddingSettlement | None:
        """在 cache/current snapshot 前校验 durable usage identity 与结果。"""

        if self._shared_budget is None:
            return None
        async with self._storage.uow() as uow:
            seed = await uow.shared_budget.usage_replay_seed(
                tenant_id=context.tenant_id,
                usage_call_id=usage_call_id,
            )
            if seed is None:
                return None
            persisted = seed.identity
            assert persisted.provider is not None
            assert persisted.model is not None
            expected = self._shared_budget.operation_identity(
                tenant_id=context.tenant_id,
                ownership_kind=seed.ownership.kind,
                run_id=context.run_id,
                agent_id=context.agent_id,
                delegation_claim_id=seed.ownership.delegation_id,
                usage_kind="embedding",
                operation_slot=usage_call_id,
                semantic_request=self._semantic_request(request),
                tree_snapshot_id=persisted.tree_snapshot_id,
                agent_sub_snapshot_id=persisted.agent_sub_snapshot_id,
                provider=persisted.provider,
                model=persisted.model,
                price_source_ref=persisted.price_source_ref,
                price_source_version=persisted.price_source_version,
                cache_key_digest=persisted.cache_key_digest,
                cost_enabled=persisted.cost_enabled,
                trusted_token_bound=persisted.trusted_token_bound,
                trusted_cost_bound=persisted.trusted_cost_bound,
            )
            uow.shared_budget.validate_usage_replay_identity(
                seed=seed,
                expected_identity=expected,
            )
            usage = await uow.evidence_outbox.replay_usage(
                tenant_id=context.tenant_id,
                run_id=context.run_id,
                agent_id=context.agent_id,
                request_id=context.request_id,
                trace_id=context.trace_id,
                usage_call_id=usage_call_id,
                event_id=self._final_event_id(context.tenant_id, usage_call_id),
                operation_kind=EvidenceOperationKind.EMBEDDING_USAGE,
            )
            if usage is None:
                raise UsageInvocationReplayError("missing_usage_settlement")
            uow.shared_budget.validate_usage_replay_settlement(
                seed=seed,
                usage_state=usage.state,
                usage_result=usage.result_json,
            )
        if seed.state == "reserved" and seed.side_effect_state == "not_started":
            return None
        return _EmbeddingSettlement(usage=usage, ownership=seed.ownership)


__all__ = ["_EmbeddingReplayMixin", "_EmbeddingSettlement", "_IdentityRuntime"]
