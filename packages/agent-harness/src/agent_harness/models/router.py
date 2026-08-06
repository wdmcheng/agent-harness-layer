"""provider-neutral 模型路由、预算上界和只缩权 seam。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from typing import cast

from agent_harness.config.schemas import ModelSettings
from agent_harness.models._router_contracts import (
    AgentModelPolicyLike,
    ModelRouteChainPlan,
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
    ModelStreamingProvider,
    ModelStructuredProvider,
    ModelToolIntentProvider,
    PreparedModelCall,
    PreparedModelStreamCall,
    PreparedModelToolIntentCall,
    PreparedStructuredModelCall,
)
from agent_harness.models.structured import OutputSchemaDefinition
from agent_harness.models.tool_catalog import ToolCatalog


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
        self._close_complete = asyncio.Event()
        self._close_failure: BaseException | None = None

    def reload(self, config: ModelRouterConfig) -> None:
        """只替换旧 fake 配置；已有 plan 不读取 reload 后的可变值。"""

        self.config = config

    async def aclose(self) -> None:
        """幂等关闭唯一 provider 实例，隐藏具体 client/factory 生命周期。"""

        if self._closed:
            await self._close_complete.wait()
            if self._close_failure is not None:
                raise RuntimeError("model router close did not complete") from self._close_failure
            return
        self._closed = True
        try:
            closed: set[int] = set()
            for provider in self._providers.values():
                identity = id(provider)
                if identity in closed:
                    continue
                closed.add(identity)
                if isinstance(provider, ModelProviderLifecycle):
                    await provider.aclose()
        except BaseException as exc:
            # 并发 close 必须观察同一失败，不能让后续 composition root 把
            # provider 未完成清理永久误认成成功并继续释放 storage。
            self._close_failure = exc
            raise
        finally:
            self._close_complete.set()

    @property
    def has_controlled_settings(self) -> bool:
        """告知 invocation 是否必须绑定 Agent policy 形成三层交集。"""

        return self._model_settings is not None and any(
            deployment.provider_kind != "fake"
            for deployment in self._model_settings.deployments.values()
        )

    @property
    def stream_chunk_utf8_bytes(self) -> int:
        """返回受 typed config 约束的项目级分片目标。"""

        return (
            self._model_settings.model_stream_chunk_utf8_bytes
            if self._model_settings is not None
            else 1024
        )

    @property
    def stream_sensitive_candidate_utf8_bytes(self) -> int:
        """返回跨片段敏感候选的项目级硬上限。"""

        return (
            self._model_settings.model_stream_sensitive_candidate_utf8_bytes
            if self._model_settings is not None
            else 512
        )

    def validate_stream_route(self, request: ModelRequest, *, plan: ModelRoutePlan) -> None:
        """在容量与 started 前证明 route/provider 支持独立流协议。"""

        if request.capability != "text_stream" or plan.capability != "text_stream":
            raise ModelRouteError(
                "model.capability_unsupported",
                "stream prepare requires text_stream capability",
            )
        provider = self._providers.get(plan.provider)
        if provider is None or not isinstance(provider, ModelStreamingProvider):
            raise ModelRouteError(
                "model.capability_unsupported",
                "bound provider does not support text streaming",
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

    def plan_tool_intent(
        self,
        request: ModelRequest,
        *,
        tool_catalog: ToolCatalog,
        agent_policy: AgentModelPolicyLike | None = None,
    ) -> ModelRoutePlan:
        """使用独立catalog参数冻结tool-enabled单route与可信输入上界。"""

        if request.capability != "tool_intent":
            raise ModelRouteError(
                "model.tool_catalog_conflict",
                "tool catalog can only be used with tool-intent capability",
            )
        if self._model_settings is None or agent_policy is None:
            raise ModelRouteError(
                "model.tool_catalog_conflict",
                "tool-intent routing requires controlled settings and agent policy",
            )
        plan = self._plan_controlled(
            request,
            agent_policy=agent_policy,
            tool_catalog=tool_catalog,
        )
        self.validate_tool_intent_route(request, plan=plan)
        return plan

    def plan_structured(
        self,
        request: ModelRequest,
        *,
        agent_policy: AgentModelPolicyLike | None = None,
    ) -> ModelRoutePlan:
        """冻结 legacy 单 route structured plan，显式 chain 不得被降级。"""

        if request.capability != "structured_output":
            raise ModelRouteError(
                "model.structured_capability_unsupported",
                "structured planning requires structured_output capability",
            )
        if agent_policy is not None and agent_policy.fallback_routes:
            raise ModelRouteError(
                "model.structured_route_not_allowed",
                "structured output does not support explicit route chains",
            )
        try:
            plan = self.plan(request, agent_policy=agent_policy)
        except ModelRouteError as exc:
            if exc.code == "model.capability_unsupported":
                raise ModelRouteError(
                    "model.structured_capability_unsupported",
                    "structured deployment capability is unavailable",
                ) from None
            raise
        self.validate_structured_route(request, plan=plan)
        return plan

    def validate_structured_route(self, request: ModelRequest, *, plan: ModelRoutePlan) -> None:
        """在 usage claim/client 前统一证明 capability 与 provider protocol。"""

        if request.capability != "structured_output" or plan.capability != "structured_output":
            raise ModelRouteError(
                "model.structured_capability_unsupported",
                "structured route capability mismatch",
            )
        provider = self._providers.get(plan.provider)
        if provider is None or not isinstance(provider, ModelStructuredProvider):
            raise ModelRouteError(
                "model.structured_capability_unsupported",
                "bound provider does not implement structured output",
            )

    def structured_repair_limit(self, plan: ModelRoutePlan) -> int:
        """返回 plan 已冻结的 repair 上限；legacy route 使用安全默认 1。"""

        return plan.max_structured_repair_attempts if self._model_settings is not None else 1

    def structured_prompt_byte_limit(self, plan: ModelRoutePlan) -> int | None:
        """受控 deployment 返回完整 prompt cap；legacy route 没有隐含无限配置。"""

        if self._model_settings is None:
            return None
        return self._model_settings.deployments[plan.deployment_id].max_prompt_utf8_bytes

    def plan_chain(
        self,
        request: ModelRequest,
        *,
        agent_policy: AgentModelPolicyLike,
    ) -> ModelRouteChainPlan:
        """显式冻结跨 deployment route chain；legacy `plan()` 语义保持不变。"""

        if self._model_settings is None:
            raise ModelRouteError(
                "model.route_not_allowed", "route chain requires controlled model settings"
            )
        return self._plan_controlled_chain(request, agent_policy=agent_policy)

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

    async def prepare_stream(
        self,
        request: ModelRequest,
        *,
        plan: ModelRoutePlan,
    ) -> PreparedModelStreamCall:
        """取得惰性流调用；不支持路径不得回退到一次性 complete。"""

        if self._closed:
            raise RuntimeError("model router is closed")
        self.validate_stream_route(request, plan=plan)
        provider = self._providers.get(plan.provider)
        assert isinstance(provider, ModelStreamingProvider)
        return await provider.prepare_stream(request, plan=plan)

    async def prepare_structured(
        self,
        request: ModelRequest,
        *,
        plan: ModelRoutePlan,
        schema: OutputSchemaDefinition,
    ) -> PreparedStructuredModelCall:
        """取得 fresh structured handle；不支持时不得回退到 text complete。"""

        if self._closed:
            raise RuntimeError("model router is closed")
        provider = self._providers.get(plan.provider)
        if provider is None or not isinstance(provider, ModelStructuredProvider):
            raise ModelRouteError(
                "model.structured_capability_unsupported",
                "bound provider does not implement structured output",
            )
        return await provider.prepare_structured(request, plan=plan, schema=schema)

    async def prepare_tool_intent(
        self,
        request: ModelRequest,
        *,
        plan: ModelRoutePlan,
    ) -> PreparedModelToolIntentCall:
        """把冻结 provider catalog bytes 交给只观察 proposal 的 adapter。"""

        if self._closed:
            raise RuntimeError("model router is closed")
        self.validate_tool_intent_route(request, plan=plan)
        provider = self._providers.get(plan.provider)
        assert isinstance(provider, ModelToolIntentProvider)
        if plan.provider_tool_catalog_json is None:
            raise ModelRouteError(
                "model.tool_catalog_conflict",
                "tool-intent plan is missing frozen provider catalog",
            )
        return await provider.prepare_tool_intent(
            request,
            plan=plan,
            tool_catalog_json=plan.provider_tool_catalog_json.encode("utf-8"),
        )


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


__all__ = [
    "ModelRouteChainPlan",
    "ModelRouteError",
    "ModelRoutePlan",
    "ModelRouter",
    "ModelRouterConfig",
]
