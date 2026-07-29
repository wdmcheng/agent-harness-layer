"""Shared-budget runtime 公开 façade；变化职责由私有 seam 隔离。"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

from agent_harness.config import HarnessSettings
from agent_harness.models.router import ModelRouterConfig
from agent_harness.runtime._shared_budget_identity import (
    delegation_identity as _delegation_identity,
)
from agent_harness.runtime._shared_budget_identity import (
    delegation_replay_identity as _delegation_replay_identity,
)
from agent_harness.runtime._shared_budget_identity import (
    operation_identity as _operation_identity,
)
from agent_harness.runtime._shared_budget_recovery import (
    embedding_price_config as _embedding_price_config,
)
from agent_harness.runtime._shared_budget_recovery import (
    model_router_config as _model_router_config,
)
from agent_harness.runtime._shared_budget_snapshot import SharedBudgetSnapshotBuilder
from agent_harness.storage.shared_budget import LedgerCreate, OperationIdentity

if TYPE_CHECKING:
    from agent_harness.registry import AgentRegistry


class SharedBudgetRuntime:
    """保留稳定公开 API，并把快照、身份与恢复投影委派给独立私有职责。"""

    def __init__(
        self,
        *,
        settings: HarnessSettings,
        registry: AgentRegistry,
        model_config: ModelRouterConfig | None = None,
        embedding_input_token_price_usd: Decimal | None = Decimal("0"),
        embedding_price_source_ref: str = "catalog:local:mock-small",
        embedding_price_source_version: str = "catalog-v1",
    ) -> None:
        """冻结组合根已校验依赖；runtime 自身不读取环境、文件或外部 provider。"""

        # 兼容既有受控快照合同对测试 registry 的定向变异；生产逻辑只经 builder 使用。
        self._registry = registry
        effective_model_config = model_config or ModelRouterConfig(
            default_provider=settings.model.provider,
            default_model=settings.model.default_model or "fake-basic",
            timeout_seconds=settings.model.timeout_seconds,
            max_tokens_per_call=settings.budget.max_tokens_per_run,
            max_cost_per_call=(
                None
                if settings.budget.max_cost_usd_per_run is None
                else Decimal(str(settings.budget.max_cost_usd_per_run))
            ),
            input_token_price_usd=Decimal("0"),
            output_token_price_usd=Decimal("0"),
            price_source_ref=f"catalog:{settings.model.provider}",
            price_source_version="catalog-v1",
        )
        self._snapshot_builder = SharedBudgetSnapshotBuilder(
            settings=settings,
            registry=registry,
            model_config=effective_model_config,
            embedding_input_token_price_usd=embedding_input_token_price_usd,
            embedding_price_source_ref=embedding_price_source_ref,
            embedding_price_source_version=embedding_price_source_version,
        )
        self._fingerprint_key = settings.budget.fingerprint_key.get_secret_value().encode("utf-8")
        self._fingerprint_key_version = settings.budget.fingerprint_key_version

    def ledger_create(self, *, tenant_id: str, run_id: str, agent_id: str) -> LedgerCreate:
        """委派构造 root frozen budget tree，保留原公开入口。"""

        return self._snapshot_builder.ledger_create(
            tenant_id=tenant_id, run_id=run_id, agent_id=agent_id
        )

    def operation_identity(self, **values: Any) -> OperationIdentity:
        """委派构造 direct operation identity。"""

        return _operation_identity(
            fingerprint_key=self._fingerprint_key,
            fingerprint_key_version=self._fingerprint_key_version,
            values=values,
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
        """委派校验冻结 target catalog 并构造 delegation identity。"""

        return _delegation_identity(
            fingerprint_key=self._fingerprint_key,
            fingerprint_key_version=self._fingerprint_key_version,
            tenant_id=tenant_id,
            canonical_request_bytes=canonical_request_bytes,
            parent_run_id=parent_run_id,
            source_agent_id=source_agent_id,
            target_agent_id=target_agent_id,
            delegation_id=delegation_id,
            idempotency_key=idempotency_key,
            tree_snapshot_id=tree_snapshot_id,
            snapshot=snapshot,
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
        """委派使用耐久不可变字段重建 delegation replay identity。"""

        return _delegation_replay_identity(
            fingerprint_key=self._fingerprint_key,
            fingerprint_key_version=self._fingerprint_key_version,
            tenant_id=tenant_id,
            canonical_request_bytes=canonical_request_bytes,
            parent_run_id=parent_run_id,
            source_agent_id=source_agent_id,
            target_agent_id=target_agent_id,
            delegation_id=delegation_id,
            idempotency_key=idempotency_key,
            persisted_identity=persisted_identity,
        )

    def model_router_config(
        self,
        *,
        snapshot: dict[str, Any],
        agent_id: str,
        base: ModelRouterConfig,
    ) -> ModelRouterConfig:
        """从冻结 target 子快照投影模型路由；允许既有 unbound 测试宿主复用。"""

        return _model_router_config(snapshot=snapshot, agent_id=agent_id, base=base)

    def embedding_price_config(
        self,
        *,
        snapshot: dict[str, Any],
        agent_id: str,
        provider: str,
        model: str,
    ) -> tuple[Decimal | None, str, str]:
        """从冻结 target 子快照投影 embedding 价格；不读取 runtime 可变状态。"""

        return _embedding_price_config(
            snapshot=snapshot,
            agent_id=agent_id,
            provider=provider,
            model=model,
        )


__all__ = ["SharedBudgetRuntime"]
