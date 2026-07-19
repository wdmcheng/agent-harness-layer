"""Root tree snapshot 冻结与 shared-budget runtime 私有依赖。"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import TYPE_CHECKING, Any, cast

from agent_harness.config import HarnessSettings
from agent_harness.models.router import ModelRouterConfig
from agent_harness.storage.shared_budget import LedgerCreate, OperationIdentity

if TYPE_CHECKING:
    from agent_harness.registry import AgentRegistry


def _digest(value: object) -> str:
    """以稳定 JSON 编码生成快照/配置摘要，禁止 NaN 造成跨进程哈希漂移。"""

    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


class SharedBudgetRuntime:
    """只在进程内持有指纹 key，并生成冻结 snapshot 与 operation identity。"""

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
        """冻结启动时的设置、registry、价格目录和仅进程内可见的指纹密钥。

        调用方传入的 model 配置仅作为 root snapshot 的来源，后续 child 运行应读取
        已持久化的冻结子快照，而不是重新查询可能已经改变的当前配置。
        """

        self._settings = settings
        self._registry = registry
        self._model_config = model_config or ModelRouterConfig(
            default_provider=settings.model.provider,
            default_model=settings.model.default_model or "fake-basic",
            timeout_seconds=settings.model.timeout_seconds,
            max_tokens_per_call=settings.budget.max_tokens_per_run,
            input_token_price_usd=Decimal("0"),
            output_token_price_usd=Decimal("0"),
            price_source_ref=f"catalog:{settings.model.provider}",
            price_source_version="catalog-v1",
        )
        self._embedding_input_token_price_usd = embedding_input_token_price_usd
        self._embedding_price_source_ref = embedding_price_source_ref
        self._embedding_price_source_version = embedding_price_source_version
        self._fingerprint_key = settings.budget.fingerprint_key.get_secret_value().encode("utf-8")

    def ledger_create(self, *, tenant_id: str, run_id: str, agent_id: str) -> LedgerCreate:
        """为 root run 构造包含可 delegation target、预算上限和价格路由的冻结账本。

        每个 target 的额度取 owner 与 target 的交集，路由版本和价格以规范摘要绑定
        到 snapshot id；这样 run 创建后的 registry 或计费目录更新不会改变其预算
        重放和结算依据。
        """

        owner = self._registry.get(agent_id)
        allowed_ids = [agent_id, *owner.delegation_targets]
        descriptors = {item_id: self._registry.get(item_id) for item_id in allowed_ids}
        agents: dict[str, Any] = {}
        for item_id, descriptor in descriptors.items():
            routes = [
                descriptor.model_policy.default_model,
                *descriptor.model_policy.fallback_models,
            ]
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
                input_price = self._model_config.route_input_token_prices_usd.get(
                    model, self._model_config.input_token_price_usd
                )
                output_price = self._model_config.route_output_token_prices_usd.get(
                    model, self._model_config.output_token_price_usd
                )
                model_routes.append(
                    {
                        "usage_kind": "model",
                        "provider": descriptor.model_policy.provider,
                        "model": model,
                        "price_source_ref": self._model_config.route_price_source_refs.get(
                            model,
                            self._model_config.price_source_ref
                            or f"catalog:{descriptor.model_policy.provider}",
                        ),
                        "price_source_version": self._model_config.route_price_source_versions.get(
                            model, self._model_config.price_source_version or "catalog-v1"
                        ),
                        "input_token_price_usd": (
                            None if input_price is None else str(input_price)
                        ),
                        "output_token_price_usd": (
                            None if output_price is None else str(output_price)
                        ),
                        "soft_max_tokens_per_call": (
                            self._model_config.route_max_tokens_per_call.get(
                                model, self._model_config.max_tokens_per_call
                            )
                        ),
                    }
                )
            agents[item_id] = {
                "agent_id": item_id,
                "descriptor_version": descriptor.version,
                "model_policy": descriptor.model_policy.to_payload(),
                "target_budget": {
                    "max_tokens_per_run": token_ceiling,
                    "max_cost_usd_per_run": cost_ceiling,
                },
                "routes": model_routes
                + [
                    {
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
                ],
            }
        registry_payload = {
            item.agent_id: item.to_payload() for item in self._registry.list_agents()
        }
        config_payload = {
            "profile": self._settings.profile,
            "model": self._settings.model.to_payload(),
            "budget": self._settings.budget.to_payload(),
        }
        registry_version = _digest(registry_payload)
        config_version = _digest(config_payload)
        catalog_version = _digest(
            {item_id: snapshot["routes"] for item_id, snapshot in agents.items()}
        )
        snapshot = {
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
        snapshot_id = f"budget-tree-v1:{_digest(snapshot)}"
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

    def operation_identity(self, **values: Any) -> OperationIdentity:
        """只消费启动时已通过 CFG-001 校验的进程内 secret bytes。"""

        return OperationIdentity.from_semantic_request(
            fingerprint_key=self._fingerprint_key,
            fingerprint_key_version=self._settings.budget.fingerprint_key_version,
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
        """把 frozen target catalog 与 0015 canonical request 绑定为顶层 identity。"""

        raw_owner = snapshot.get("owner")
        raw_agents = snapshot.get("agents")
        if not isinstance(raw_owner, dict) or not isinstance(raw_agents, dict):
            raise ValueError("shared budget delegation snapshot is invalid")
        owner = cast(dict[str, object], raw_owner)
        agents = cast(dict[str, object], raw_agents)
        raw_target = agents.get(target_agent_id)
        raw_targets = owner.get("delegation_targets")
        if (
            owner.get("agent_id") != source_agent_id
            or owner.get("root_run_id") != parent_run_id
            or not isinstance(raw_targets, list)
            or target_agent_id not in raw_targets
            or not isinstance(raw_target, dict)
        ):
            raise ValueError("shared budget delegation snapshot is invalid")
        target = cast(dict[str, object], raw_target)
        raw_routes = target.get("routes")
        if not isinstance(raw_routes, list) or not raw_routes:
            raise ValueError("shared budget delegation target catalog is invalid")
        routes = cast(list[object], raw_routes)
        cost_enabled = owner.get("cost_enabled")
        if not isinstance(cost_enabled, bool):
            raise ValueError("shared budget delegation cost mode is invalid")
        return OperationIdentity.from_delegation_request(
            tenant_id=tenant_id,
            fingerprint_key=self._fingerprint_key,
            fingerprint_key_version=self._settings.budget.fingerprint_key_version,
            canonical_request_bytes=canonical_request_bytes,
            parent_run_id=parent_run_id,
            source_agent_id=source_agent_id,
            target_agent_id=target_agent_id,
            delegation_claim_id=delegation_id,
            operation_slot=idempotency_key,
            tree_snapshot_id=tree_snapshot_id,
            target_sub_snapshot_id=f"{tree_snapshot_id}:{target_agent_id}",
            target_route_catalog_digest=f"budget-routes-v1:{_digest(routes)}",
            cost_enabled=cost_enabled,
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
        """只用 durable immutable fields 重算请求身份，不依赖当前 snapshot。"""

        if (
            persisted_identity.ownership_kind != "delegation"
            or persisted_identity.target_route_catalog_digest is None
        ):
            raise ValueError("shared budget delegation replay identity is invalid")
        return OperationIdentity.from_delegation_request(
            tenant_id=tenant_id,
            fingerprint_key=self._fingerprint_key,
            fingerprint_key_version=self._settings.budget.fingerprint_key_version,
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

    def model_router_config(
        self,
        *,
        snapshot: dict[str, Any],
        agent_id: str,
        base: ModelRouterConfig,
    ) -> ModelRouterConfig:
        """只从 frozen target sub-snapshot 投影当前 agent 的实际 model route。"""

        raw_agents: object = snapshot.get("agents")
        if not isinstance(raw_agents, dict):
            raise ValueError("shared budget target model snapshot is invalid")
        agents = cast(dict[str, object], raw_agents)
        raw_target = agents.get(agent_id)
        if not isinstance(raw_target, dict):
            raise ValueError("shared budget target model snapshot is invalid")
        target = cast(dict[str, object], raw_target)
        raw_policy = target.get("model_policy")
        raw_routes = target.get("routes")
        if not isinstance(raw_policy, dict) or not isinstance(raw_routes, list):
            raise ValueError("shared budget target model snapshot is invalid")
        policy = cast(dict[str, object], raw_policy)
        routes = cast(list[object], raw_routes)
        provider = policy.get("provider")
        default_model = policy.get("default_model")
        raw_fallback_models = policy.get("fallback_models")
        fallback_values = (
            cast(list[object], raw_fallback_models) if isinstance(raw_fallback_models, list) else []
        )
        if (
            not isinstance(provider, str)
            or not provider
            or not isinstance(default_model, str)
            or not default_model
            or not isinstance(raw_fallback_models, list)
            or any(not isinstance(item, str) or not item for item in fallback_values)
        ):
            raise ValueError("shared budget target model policy is invalid")
        fallback_models = [cast(str, item) for item in fallback_values]
        route_refs: dict[str, str] = {}
        route_versions: dict[str, str] = {}
        route_input_prices: dict[str, Decimal] = {}
        route_output_prices: dict[str, Decimal] = {}
        route_limits: dict[str, int] = {}
        for raw_route in routes:
            if not isinstance(raw_route, dict):
                continue
            raw = cast(dict[str, object], raw_route)
            if raw.get("usage_kind") != "model" or raw.get("provider") != provider:
                continue
            model = raw.get("model")
            ref = raw.get("price_source_ref")
            version = raw.get("price_source_version")
            if (
                not isinstance(model, str)
                or not isinstance(ref, str)
                or not isinstance(version, str)
            ):
                continue
            route_refs[model] = ref
            route_versions[model] = version
            input_price = SharedBudgetRuntime._price(raw.get("input_token_price_usd"))
            output_price = SharedBudgetRuntime._price(raw.get("output_token_price_usd"))
            if input_price is not None:
                route_input_prices[model] = input_price
            if output_price is not None:
                route_output_prices[model] = output_price
            soft_limit = raw.get("soft_max_tokens_per_call")
            if isinstance(soft_limit, int) and not isinstance(soft_limit, bool) and soft_limit >= 0:
                route_limits[model] = soft_limit
        allowed = {default_model, *fallback_models}
        if not allowed <= set(route_refs):
            raise ValueError("shared budget target route catalog is incomplete")
        raw_owner = snapshot.get("owner")
        if not isinstance(raw_owner, dict):
            raise ValueError("shared budget owner snapshot is invalid")
        cost_enabled = cast(dict[str, object], raw_owner).get("cost_enabled")
        if cost_enabled is True and (
            not allowed <= set(route_input_prices) or not allowed <= set(route_output_prices)
        ):
            raise ValueError("shared budget target route price is incomplete")
        default_limit = route_limits.get(default_model)
        return base.model_copy(
            update={
                "default_provider": provider,
                "default_model": default_model,
                "fallback_models": list(fallback_models),
                "max_tokens_per_call": default_limit,
                "input_token_price_usd": route_input_prices.get(default_model),
                "output_token_price_usd": route_output_prices.get(default_model),
                "price_source_ref": route_refs[default_model],
                "price_source_version": route_versions[default_model],
                "route_price_source_refs": route_refs,
                "route_price_source_versions": route_versions,
                "route_input_token_prices_usd": route_input_prices,
                "route_output_token_prices_usd": route_output_prices,
                "route_max_tokens_per_call": route_limits,
            }
        )

    def embedding_price_config(
        self,
        *,
        snapshot: dict[str, Any],
        agent_id: str,
        provider: str,
        model: str,
    ) -> tuple[Decimal | None, str, str]:
        """从 target sub-snapshot 解析 embedding 的冻结价格。"""

        raw_agents = snapshot.get("agents")
        raw_owner = snapshot.get("owner")
        if not isinstance(raw_agents, dict) or not isinstance(raw_owner, dict):
            raise ValueError("shared budget embedding snapshot is invalid")
        raw_target = cast(dict[str, object], raw_agents).get(agent_id)
        if not isinstance(raw_target, dict):
            raise ValueError("shared budget embedding snapshot is invalid")
        raw_routes = cast(dict[str, object], raw_target).get("routes")
        if not isinstance(raw_routes, list):
            raise ValueError("shared budget embedding snapshot is invalid")
        for raw_route in cast(list[object], raw_routes):
            if not isinstance(raw_route, dict):
                continue
            route = cast(dict[str, object], raw_route)
            if (
                route.get("usage_kind") != "embedding"
                or route.get("provider") != provider
                or route.get("model") != model
            ):
                continue
            ref = route.get("price_source_ref")
            version = route.get("price_source_version")
            if not isinstance(ref, str) or not ref or not isinstance(version, str) or not version:
                break
            price = SharedBudgetRuntime._price(route.get("input_token_price_usd"))
            return price, ref, version
        raise ValueError("shared budget embedding route price is incomplete")

    @staticmethod
    def _price(value: object) -> Decimal | None:
        """把快照 JSON 中的可选价格还原为有限非负 Decimal，损坏值一律 fail-closed。"""

        if value is None:
            return None
        try:
            price = Decimal(str(value))
        except Exception as exc:  # noqa: BLE001 - snapshot JSON 边界必须 fail closed
            raise ValueError("shared budget route price is invalid") from exc
        if not price.is_finite() or price < 0:
            raise ValueError("shared budget route price is invalid")
        return price


__all__ = ["SharedBudgetRuntime"]
