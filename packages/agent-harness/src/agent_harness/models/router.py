"""provider-neutral 模型路由、预算上界和只缩权 seam。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import cast

from agent_harness.config.schemas import ModelSettings
from agent_harness.models._router_contracts import (
    AgentModelPolicyLike,
    ModelRouteError,
    ModelRoutePlan,
    ModelRouterConfig,
)
from agent_harness.models._router_snapshot import RouterSnapshotPlanningMixin
from agent_harness.models.providers import (
    ModelProvider,
    ModelProviderLifecycle,
    ModelRequest,
    ModelResponse,
    PreparedModelCall,
)


class ModelRouter(RouterSnapshotPlanningMixin):
    """先冻结 route plan，再由 async provider 执行；request 不能扩大配置权限。"""

    def __init__(
        self,
        *,
        config: ModelRouterConfig,
        providers: Mapping[str, ModelProvider],
        model_settings: ModelSettings | None = None,
    ) -> None:
        self.config = config
        self._providers = dict(providers)
        self._model_settings = model_settings
        self._closed = False

    def reload(self, config: ModelRouterConfig) -> None:
        """只替换旧 fake 配置；已有 plan 不读取 reload 后的可变值。"""

        self.config = config

    async def aclose(self) -> None:
        """幂等关闭唯一 provider 实例，隐藏具体 client/factory 生命周期。"""

        if self._closed:
            return
        self._closed = True
        closed: set[int] = set()
        for provider in self._providers.values():
            identity = id(provider)
            if identity in closed:
                continue
            closed.add(identity)
            if isinstance(provider, ModelProviderLifecycle):
                await provider.aclose()

    @property
    def has_controlled_settings(self) -> bool:
        """告知 invocation 是否必须绑定 Agent policy 形成三层交集。"""

        return self._model_settings is not None and any(
            deployment.provider_kind != "fake"
            for deployment in self._model_settings.deployments.values()
        )

    async def route(
        self,
        request: ModelRequest,
        *,
        agent_policy: AgentModelPolicyLike | None = None,
    ) -> ModelResponse:
        """兼容入口同样为 async，禁止线程或嵌套 event loop 桥接。"""

        plan = self.plan(request, agent_policy=agent_policy)
        return await self.execute(request, plan=plan)

    def plan(
        self,
        request: ModelRequest,
        *,
        config: ModelRouterConfig | None = None,
        agent_policy: AgentModelPolicyLike | None = None,
    ) -> ModelRoutePlan:
        """在预算、client、DNS/HTTP 前完成 route 交集与可信公式计算。"""

        if self._model_settings is not None and agent_policy is not None:
            return self._plan_controlled(
                request,
                agent_policy=agent_policy,
            )
        if self.has_controlled_settings:
            # 只要进程装载了真实 deployment，Agent policy 就是三层缩权不可缺失的
            # 授权输入；回退 legacy 会让公共 Router 绕过 deployment∩Agent 交集。
            raise ModelRouteError(
                "model.route_not_allowed",
                "controlled routing requires an agent model policy",
            )
        return self._plan_legacy_fake(request, config=config)

    async def execute(self, request: ModelRequest, *, plan: ModelRoutePlan) -> ModelResponse:
        """执行已冻结 plan；后续实现层负责 reservation/permit/mark 顺序。"""

        if plan.decision.action == "policy_required":
            return ModelResponse(
                provider=plan.provider,
                model=plan.model,
                output_text="",
                decision=plan.decision,
                token_usage={},
            )
        routed_request = request.model_copy(
            update={
                "deployment_id": plan.deployment_id,
                "provider": plan.provider,
                "model": plan.model,
                "max_output_tokens": plan.output_token_cap,
                "timeout_seconds": max(1, plan.total_timeout_ms // 1000),
            }
        )
        prepared = await self.prepare(routed_request, plan=plan)
        try:
            response = await prepared.send()
        finally:
            await prepared.aclose()
        return self.normalize_response(response, plan=plan)

    def normalize_response(
        self,
        response: ModelResponse,
        *,
        plan: ModelRoutePlan,
    ) -> ModelResponse:
        """按冻结 plan 复核 provider identity，并覆盖不可由 adapter 决定的路由事实。"""

        if response.provider != plan.provider or response.model != plan.model:
            raise ModelRouteError("model.provider_failed", "provider response identity mismatch")
        normalized = response.decision.model_copy(
            update={
                "action": plan.decision.action,
                "estimated_tokens": plan.decision.estimated_tokens,
                "max_tokens": plan.decision.max_tokens,
                "estimated_cost_usd": plan.decision.estimated_cost_usd,
                "max_cost_usd": plan.decision.max_cost_usd,
                "fallback_model": plan.decision.fallback_model,
                "reason": plan.decision.reason or response.decision.reason,
                "price_source_ref": plan.price_source_ref or response.decision.price_source_ref,
                "price_source_version": (
                    plan.price_source_version or response.decision.price_source_version
                ),
            }
        )
        return response.model_copy(update={"decision": normalized})

    async def prepare(
        self,
        request: ModelRequest,
        *,
        plan: ModelRoutePlan,
    ) -> PreparedModelCall:
        """取得 permit/client 但不发送；旧 provider 由窄包装保持 async 兼容。"""

        if self._closed:
            raise RuntimeError("model router is closed")
        provider = self._providers[plan.provider]
        prepare = getattr(provider, "prepare", None)
        if callable(prepare):
            typed_prepare = cast(
                Callable[..., Awaitable[PreparedModelCall]],
                prepare,
            )
            return await typed_prepare(request, plan=plan)
        return _DirectPreparedCall(provider=provider, request=request, plan=plan)


class _DirectPreparedCall:
    """无独立 prepare seam 的 fake/测试 provider 兼容包装。"""

    def __init__(
        self, *, provider: ModelProvider, request: ModelRequest, plan: ModelRoutePlan
    ) -> None:
        self._provider = provider
        self._request = request
        self._plan = plan

    async def send(self) -> ModelResponse:
        """委托现有 async provider；该包装不构造 client。"""

        return await self._provider.complete(self._request, plan=self._plan)

    async def aclose(self) -> None:
        """兼容包装没有独占资源。"""


__all__ = ["ModelRouteError", "ModelRoutePlan", "ModelRouter", "ModelRouterConfig"]
