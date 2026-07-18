"""Root tree snapshot 冻结与 shared-budget runtime 私有依赖。"""

from __future__ import annotations

import hashlib
import json
import os
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from agent_harness.config import HarnessSettings
from agent_harness.models.router import ModelRouterConfig
from agent_harness.storage.shared_budget import LedgerCreate, OperationIdentity

if TYPE_CHECKING:
    from agent_harness.registry import AgentRegistry


def _digest(value: object) -> str:
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

    def ledger_create(self, *, tenant_id: str, run_id: str, agent_id: str) -> LedgerCreate:
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
        """从进程 secret 或 secret file 读取 key，缺失时在 reservation 前失败。"""

        env_name = self._settings.budget.fingerprint_key_env
        secret = os.environ.get(env_name)
        if not secret:
            file_env_name = self._settings.budget.fingerprint_key_file_env
            secret_path = os.environ.get(file_env_name)
            if secret_path:
                try:
                    secret = Path(secret_path).read_text(encoding="utf-8").strip()
                except OSError as exc:
                    raise ValueError("shared budget fingerprint key file is unavailable") from exc
        if not secret:
            raise ValueError("shared budget fingerprint key is not configured")
        return OperationIdentity.from_semantic_request(
            fingerprint_key=secret.encode("utf-8"),
            fingerprint_key_version=self._settings.budget.fingerprint_key_version,
            **values,
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
