"""Route-chain streaming 的路由预检与 usage evidence 支持。"""

from __future__ import annotations

from collections.abc import Callable

from agent_harness.models._router_contracts import ModelRouteChainPlan
from agent_harness.models.providers import ModelRequest
from agent_harness.models.router import ModelRouter
from agent_harness.models.usage import ModelUsageEvidence


def validate_chain_stream_routes(
    router: ModelRouter,
    request: ModelRequest,
    chain: ModelRouteChainPlan,
) -> None:
    """在建立耐久 claim 前逐候选验证 streaming capability 与请求缩权。"""

    for candidate in chain.candidates:
        routed_request = request.model_copy(
            update={
                "deployment_id": candidate.deployment_id,
                "provider": candidate.provider,
                "model": candidate.model,
                "route_refs": None,
                "max_output_tokens": candidate.route.output_token_cap,
            }
        )
        router.validate_stream_route(routed_request, plan=candidate.route)


def with_stream_usage_identity(
    evidence: ModelUsageEvidence,
    *,
    safe_decision: Callable[..., dict[str, object]],
) -> ModelUsageEvidence:
    """为 route-chain usage 选择与 stream 容量占位一致的稳定事件 identity。"""

    return evidence.model_copy(
        update={
            "decision": safe_decision(
                evidence.decision,
                {"usage_event_identity": {"ref": "stream-usage", "version": "v1"}},
            )
        }
    )


__all__ = ["validate_chain_stream_routes", "with_stream_usage_identity"]
