"""Shared-budget root tree 快照构建私有 seam。"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

from agent_harness.config import HarnessSettings
from agent_harness.config.model_endpoints import resolve_model_deployment
from agent_harness.models.router import ModelRouterConfig
from agent_harness.runtime._shared_budget_common import digest
from agent_harness.storage.shared_budget import LedgerCreate

if TYPE_CHECKING:
    from agent_harness.registry import AgentRegistry


class SharedBudgetSnapshotBuilder:
    """只负责从启动时配置与 registry 构造不可变预算树快照。"""

    def __init__(
        self,
        *,
        settings: HarnessSettings,
        registry: AgentRegistry,
        model_config: ModelRouterConfig,
        embedding_input_token_price_usd: Decimal | None,
        embedding_price_source_ref: str,
        embedding_price_source_version: str,
    ) -> None:
        """保存组合根已校验的依赖；构建期间不读取环境或可变全局状态。"""

        self._settings = settings
        self._registry = registry
        self._model_config = model_config
        self._embedding_input_token_price_usd = embedding_input_token_price_usd
        self._embedding_price_source_ref = embedding_price_source_ref
        self._embedding_price_source_version = embedding_price_source_version

    def ledger_create(self, *, tenant_id: str, run_id: str, agent_id: str) -> LedgerCreate:
        """为 root run 构造包含授权 target、预算上限和价格路由的冻结账本。"""

        owner = self._registry.get(agent_id)
        allowed_ids = [agent_id, *owner.delegation_targets]
        descriptors = {item_id: self._registry.get(item_id) for item_id in allowed_ids}
        # 未被本 root/child tree 引用的真实 deployment 不得把纯 fake 运行升级为 v2，
        # 否则仅仅配置一个 opt-in 真实入口就会破坏默认离线 agent 的旧快照路径。
        controlled_v2 = any(
            self._settings.model.deployments[descriptor.model_policy.deployment_id].provider_kind
            != "fake"
            for descriptor in descriptors.values()
        )
        agents: dict[str, Any] = {}
        for item_id, descriptor in descriptors.items():
            routes = (
                list(descriptor.model_policy.allowed_models)
                if controlled_v2
                else [
                    descriptor.model_policy.default_model,
                    *descriptor.model_policy.fallback_models,
                ]
            )
            routes = list(dict.fromkeys(routes))
            token_ceiling = min(
                owner.budget.max_tokens_per_run,
                descriptor.budget.max_tokens_per_run,
            )
            owner_cost = owner.budget.max_cost_usd_per_run
            target_cost = descriptor.budget.max_cost_usd_per_run
            cost_ceiling = (
                None
                if owner_cost is None
                else min(owner_cost, target_cost)
                if target_cost is not None
                else owner_cost
            )
            model_routes: list[dict[str, Any]] = []
            for model in routes:
                if controlled_v2:
                    model_routes.append(
                        self._controlled_model_route(
                            descriptor=descriptor,
                            model=model,
                        )
                    )
                    continue
                model_routes.append(
                    self._legacy_model_route(
                        provider=descriptor.model_policy.provider,
                        model=model,
                    )
                )
            policy_payload = descriptor.model_policy.to_payload()
            if controlled_v2:
                resolved_policy = resolve_model_deployment(
                    self._settings.model,
                    descriptor.model_policy.deployment_id,
                )
                # Agent 与 deployment 的 fallback 顺序都进入 durable policy；恢复时
                # 只能取两者交集，不能读取 reload 后配置补齐或扩大候选。
                policy_payload["deployment_fallback_models"] = list(resolved_policy.fallback_models)
            agents[item_id] = {
                "agent_id": item_id,
                "descriptor_version": descriptor.version,
                "model_policy": policy_payload,
                "target_budget": {
                    "max_tokens_per_run": token_ceiling,
                    "max_cost_usd_per_run": cost_ceiling,
                },
                "routes": model_routes + [self._embedding_route()],
            }
        registry_payload = {
            item.agent_id: item.to_payload() for item in self._registry.list_agents()
        }
        config_payload = {
            "profile": self._settings.profile,
            "model": self._settings.model.to_payload(),
            "budget": self._settings.budget.to_payload(),
        }
        registry_version = digest(registry_payload)
        config_version = digest(config_payload)
        catalog_version = digest(
            {item_id: snapshot["routes"] for item_id, snapshot in agents.items()}
        )
        snapshot = {
            "schema_version": "budget-tree-v2" if controlled_v2 else "budget-tree-v1",
            "owner": {
                "agent_id": agent_id,
                "root_run_id": run_id,
                "delegation_targets": list(owner.delegation_targets),
                "max_tokens_per_run": owner.budget.max_tokens_per_run,
                "max_cost_usd_per_run": owner.budget.max_cost_usd_per_run,
                "cost_enabled": owner.budget.max_cost_usd_per_run is not None,
            },
            "registry_version": registry_version,
            "config_version": config_version,
            "catalog_version": catalog_version,
            "agents": agents,
        }
        snapshot_id = f"{snapshot['schema_version']}:{digest(snapshot)}"
        return LedgerCreate(
            tenant_id=tenant_id,
            budget_owner_run_id=run_id,
            token_limit=owner.budget.max_tokens_per_run,
            cost_limit=(
                None
                if owner.budget.max_cost_usd_per_run is None
                else Decimal(str(owner.budget.max_cost_usd_per_run))
            ),
            registry_version=registry_version,
            config_version=config_version,
            catalog_version=catalog_version,
            snapshot_id=snapshot_id,
            snapshot=snapshot,
        )

    def _controlled_model_route(self, *, descriptor: Any, model: str) -> dict[str, Any]:
        """把受控 deployment/catalog 投影为完整耐久恢复路由。"""

        deployment_id = descriptor.model_policy.deployment_id
        resolved = resolve_model_deployment(self._settings.model, deployment_id)
        if (
            descriptor.model_policy.provider != resolved.provider_kind
            or model not in resolved.allowed_models
        ):
            raise ValueError("shared budget controlled route is not allowed")
        deployment = self._settings.model.deployments[deployment_id]
        catalog = resolved.model_catalogs[model]
        route_token_ceiling = (
            deployment.max_prompt_utf8_bytes
            + catalog.input_envelope_token_bound
            + deployment.max_output_tokens
        )
        route_cost_ceiling: Decimal | None = None
        if catalog.cost_enabled:
            assert catalog.input_token_price_usd is not None
            assert catalog.output_token_price_usd is not None
            route_cost_ceiling = (
                Decimal(deployment.max_prompt_utf8_bytes + catalog.input_envelope_token_bound)
                * catalog.input_token_price_usd
                + Decimal(deployment.max_output_tokens) * catalog.output_token_price_usd
            )
        return {
            "usage_kind": "model",
            "deployment_id": deployment_id,
            "provider": resolved.provider_kind,
            "model": model,
            # canonical path 属于私有恢复身份；公开 evidence 只投影 origin。
            "canonical_base_url": resolved.canonical_base_url,
            "endpoint_origin": resolved.endpoint_origin,
            "endpoint_policy_ref": resolved.endpoint_policy_ref,
            "endpoint_policy_version": resolved.endpoint_policy_version,
            "endpoint_policy_digest": resolved.endpoint_policy_digest,
            "completion_classifier_ref": deployment.completion_classifier_ref,
            "completion_classifier_version": deployment.completion_classifier_version,
            "credential_ref": resolved.credential_ref,
            "capabilities": list(deployment.capabilities),
            "model_catalog_ref": deployment.model_catalog_refs[model],
            "model_catalog_version": catalog.version,
            "model_catalog_digest": catalog.digest,
            "request_shape_ref": catalog.request_shape_ref,
            "request_shape_version": catalog.request_shape_version,
            "input_bound_strategy_ref": catalog.input_bound_strategy_ref,
            "input_bound_strategy_version": catalog.input_bound_strategy_version,
            "input_envelope_token_bound": catalog.input_envelope_token_bound,
            "cost_enabled": catalog.cost_enabled,
            "price_source_ref": catalog.price_source_ref,
            "price_source_version": catalog.price_source_version,
            "input_token_price_usd": (
                None
                if catalog.input_token_price_usd is None
                else str(catalog.input_token_price_usd)
            ),
            "output_token_price_usd": (
                None
                if catalog.output_token_price_usd is None
                else str(catalog.output_token_price_usd)
            ),
            "max_prompt_utf8_bytes": deployment.max_prompt_utf8_bytes,
            "max_output_tokens": deployment.max_output_tokens,
            "max_per_attempt_token_bound": route_token_ceiling,
            "max_per_attempt_cost_bound": (
                None if route_cost_ceiling is None else str(route_cost_ceiling)
            ),
            "max_attempts": deployment.max_attempts,
            "connect_timeout_ms": deployment.connect_timeout_ms,
            "read_timeout_ms": deployment.read_timeout_ms,
            "total_timeout_ms": deployment.total_timeout_ms,
            "retry_policy": {
                "retryable_http_statuses": list(deployment.retryable_http_statuses),
                "max_attempts": deployment.max_attempts,
                "max_wait_ms": deployment.max_retry_wait_ms,
                "backoff_initial_ms": deployment.backoff_initial_ms,
                "backoff_max_ms": deployment.backoff_max_ms,
            },
            "bulkhead_policy": {
                "scope": "process_deployment",
                "max_in_flight": deployment.max_in_flight,
                "queue_timeout_ms": deployment.queue_timeout_ms,
            },
            # 旧投影仍消费该字段；v2 恢复使用精确静态上界字段。
            "soft_max_tokens_per_call": route_token_ceiling,
        }

    def _legacy_model_route(self, *, provider: str, model: str) -> dict[str, Any]:
        """保持纯 fake/local v1 快照的历史字段与默认值。"""

        input_price = self._model_config.route_input_token_prices_usd.get(
            model, self._model_config.input_token_price_usd
        )
        output_price = self._model_config.route_output_token_prices_usd.get(
            model, self._model_config.output_token_price_usd
        )
        return {
            "usage_kind": "model",
            "provider": provider,
            "model": model,
            "price_source_ref": self._model_config.route_price_source_refs.get(
                model,
                self._model_config.price_source_ref or f"catalog:{provider}",
            ),
            "price_source_version": self._model_config.route_price_source_versions.get(
                model, self._model_config.price_source_version or "catalog-v1"
            ),
            "input_token_price_usd": None if input_price is None else str(input_price),
            "output_token_price_usd": None if output_price is None else str(output_price),
            "soft_max_tokens_per_call": self._model_config.route_max_tokens_per_call.get(
                model, self._model_config.max_tokens_per_call
            ),
        }

    def _embedding_route(self) -> dict[str, Any]:
        """构造每个 agent 子快照共享的本地 embedding 价格路由。"""

        return {
            "usage_kind": "embedding",
            "provider": "local",
            "model": "mock-small",
            "price_source_ref": self._embedding_price_source_ref,
            "price_source_version": self._embedding_price_source_version,
            "input_token_price_usd": (
                None
                if self._embedding_input_token_price_usd is None
                else str(self._embedding_input_token_price_usd)
            ),
        }


__all__ = ["SharedBudgetSnapshotBuilder"]
