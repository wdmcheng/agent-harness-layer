"""模型调用中的策略审批与 provider tool-intent 归一化协作者。"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, cast

from agent_harness.contracts.trust import GuardrailDecisionStatus
from agent_harness.identity import IdentityContext
from agent_harness.models._invocation_settlement import ModelProviderInvocationError
from agent_harness.models._invocation_tool_intent_approval import (
    model_request_arguments_hash,
    tool_intent_approval_arguments,
    tool_intent_approval_arguments_hash,
    tool_intent_approval_continuation,
)
from agent_harness.models._router_contracts import ModelRoutePlan
from agent_harness.models.providers import (
    ModelRequest,
    ModelResponse,
    StructuredProviderCandidate,
)
from agent_harness.models.tool_catalog import ToolCatalog
from agent_harness.models.tool_intent import (
    ModelTurnResult,
    ProviderToolIntentCandidate,
    ToolIntentReplaySeed,
    ToolIntentTurnResult,
    ToolIntentValidationError,
    normalize_provider_tool_intent,
)
from agent_harness.models.usage import ModelUsageEvidence, UsageEvidenceContext
from agent_harness.policy import PolicyCheck, PolicyEngine

if TYPE_CHECKING:
    from agent_harness.runtime.executor import AgentApprovalRequest


class ModelApprovalRequired(RuntimeError):
    """策略要求 durable 审批时返回的受控暂停信号。"""

    code = "model.approval_required"

    def __init__(self, request: AgentApprovalRequest) -> None:
        super().__init__(self.code)
        self.request = request


class ModelLoopReservationError(RuntimeError):
    """loop 剩余量不能覆盖本轮 route reservation 时的副作用前终止。"""

    def __init__(self, code: str) -> None:
        """只允许 loop façade 识别的稳定 limit/needs-review 错误码。"""

        if code not in {
            "model.tool_loop_limit_exceeded",
            "model.tool_loop_needs_review",
        }:
            raise ValueError("invalid model tool loop reservation error code")
        super().__init__(code)
        self.code = code


def validate_loop_reservation_bounds(
    *,
    tool_loop_id: str | None,
    tool_turn_ordinal: int | None,
    operation_identity_digest: str | None,
    token_bound: int | None,
    cost_bound: float | None,
) -> bool:
    """在 replay、catalog 与 provider 副作用前验证完整 loop 身份和预约边界。"""

    tool_mode = tool_loop_id is not None or tool_turn_ordinal is not None
    if tool_mode != (tool_loop_id is not None and tool_turn_ordinal is not None):
        raise ValueError("tool loop identity must be complete")
    if tool_mode and operation_identity_digest is None:
        raise ValueError("tool operation identity must be complete")
    if tool_mode and token_bound is not None:
        if type(token_bound) is not int or token_bound < 1:
            raise ModelLoopReservationError("model.tool_loop_needs_review")
        if cost_bound is not None and (
            type(cost_bound) is not float or not math.isfinite(cost_bound) or cost_bound < 0
        ):
            raise ModelLoopReservationError("model.tool_loop_needs_review")
    elif cost_bound is not None:
        if tool_mode:
            raise ModelLoopReservationError("model.tool_loop_needs_review")
        raise ValueError("loop reservation bounds require tool loop identity")
    if not tool_mode and token_bound is not None:
        raise ValueError("loop reservation bounds require tool loop identity")
    return tool_mode


async def model_policy_approval_request(
    *,
    policy_engine: PolicyEngine | None,
    soft_approved: bool,
    actor: IdentityContext | None,
    context: UsageEvidenceContext,
    plan: ModelRoutePlan,
    request: ModelRequest,
    usage_call_id: str,
    replay_seed: ToolIntentReplaySeed | None,
) -> AgentApprovalRequest | None:
    """执行 provider 副作用前的三态 Policy gate，并只返回 data-only 审批请求。"""

    if policy_engine is None or soft_approved:
        return None
    if actor is None:
        raise RuntimeError("model policy requires bound identity")
    policy = await policy_engine.evaluate(
        PolicyCheck(
            actor=actor,
            action="model.invoke",
            resource=f"agent:{context.agent_id}:model",
            context={
                "tenant_id": context.tenant_id,
                "agent_id": context.agent_id,
                "run_id": context.run_id,
                "request_id": context.request_id,
                "trace_id": context.trace_id,
                "deployment_id": plan.deployment_id,
                "provider": plan.provider,
                "model": plan.model,
                "model_catalog_ref": plan.model_catalog_ref,
                "model_catalog_version": plan.model_catalog_version,
                "model_catalog_digest": plan.model_catalog_digest,
                "reserved_token_bound": plan.reserved_token_bound,
                "reserved_cost_bound": (
                    None if plan.reserved_cost_bound is None else float(plan.reserved_cost_bound)
                ),
                "soft_decision": plan.decision.action,
                **(
                    {
                        "tool_request_identity_digest": replay_seed.request_identity.digest,
                        "tool_catalog_digest": replay_seed.tool_catalog.catalog_digest,
                        "tool_loop_id": replay_seed.loop_id,
                        "tool_turn_ordinal": replay_seed.turn_ordinal,
                        "tool_operation_identity_digest": replay_seed.operation_identity_digest,
                    }
                    if replay_seed is not None
                    else {}
                ),
            },
        )
    )
    if policy.decision == GuardrailDecisionStatus.DENY.value:
        raise ModelProviderInvocationError("model.policy_denied")
    if policy.decision != GuardrailDecisionStatus.REQUIRE_APPROVAL.value:
        return None

    # 延迟导入避免 models/runtime 公开 façade 初始化形成环；DTO 仍复用唯一
    # 既有审批状态机类型，协作者不创建第二审批协议。
    from agent_harness.runtime.executor import AgentApprovalRequest

    if replay_seed is None:
        arguments_hash = hashlib.sha256(
            json.dumps(
                request.to_payload(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        continuation = {
            "kind": "policy_approval",
            "deployment_id": plan.deployment_id,
            "model": plan.model,
        }
    else:
        approval_arguments = tool_intent_approval_arguments(
            usage_call_id=usage_call_id,
            operation_identity_digest=replay_seed.operation_identity_digest,
            request_arguments_hash=model_request_arguments_hash(request),
            tool_request_identity_digest=replay_seed.request_identity.digest,
        )
        arguments_hash = tool_intent_approval_arguments_hash(approval_arguments)
        continuation = tool_intent_approval_continuation(
            arguments=approval_arguments,
            replay_seed=replay_seed,
        )
    return AgentApprovalRequest(
        action="model.invoke",
        resource=f"agent:{context.agent_id}:model",
        reason=policy.reason,
        arguments_ref=f"model-request:{arguments_hash}",
        arguments_hash=arguments_hash,
        continuation=continuation,
    )


def normalize_tool_turn(
    raw_turn: object,
    *,
    plan: ModelRoutePlan,
    catalog: ToolCatalog,
    loop_id: str,
    turn_ordinal: int,
    usage_call_id: str,
) -> tuple[ModelResponse, ModelTurnResult | None]:
    """在副作用后重验不受信 adapter 结果，并投影 provider-neutral turn。"""

    if type(raw_turn) is ModelResponse:
        return (
            ModelResponse.model_validate(raw_turn.model_dump(mode="python")),
            None,
        )
    candidate = ProviderToolIntentCandidate.validated_snapshot(raw_turn)
    if candidate is None:
        failure = ToolIntentValidationError()
        # 另一 capability 的 exact candidate 仍是协议违规，但已观测 attempt
        # 必须保留，不能因 capability 分支拒绝而被抹成 unknown。
        if type(raw_turn) is StructuredProviderCandidate:
            try:
                structured_candidate = StructuredProviderCandidate.model_validate(
                    raw_turn.model_dump(mode="python")
                )
            except (AttributeError, TypeError, ValueError):
                pass
            else:
                failure.attempts = structured_candidate.attempts
        raise failure
    entry = next((item for item in catalog.tools if item.name == candidate.tool_name), None)
    if entry is None:
        failure = ToolIntentValidationError()
        failure.attempts = candidate.attempts
        raise failure
    try:
        intent = normalize_provider_tool_intent(
            candidate,
            expected_provider=plan.provider,
            expected_model=plan.model,
            expected_tool_name=entry.name,
            expected_tool_schema_ref=entry.input_schema_ref,
            expected_tool_schema_version=entry.input_schema_version,
            expected_tool_schema_digest=entry.input_schema_digest,
            loop_id=loop_id,
            turn_ordinal=turn_ordinal,
            model_usage_call_id=usage_call_id,
            catalog_digest=catalog.catalog_digest,
        )
    except ToolIntentValidationError as exc:
        exc.attempts = candidate.attempts
        raise
    attempt = candidate.attempts[0]
    return (
        ModelResponse(
            provider=candidate.provider,
            model=candidate.model,
            output_text="",
            decision=plan.decision,
            token_usage=(
                {
                    "input_tokens": attempt.input_tokens,
                    "output_tokens": attempt.output_tokens,
                }
                if attempt.input_tokens is not None and attempt.output_tokens is not None
                else {}
            ),
            latency_ms=attempt.latency_ms,
            cost_usd=attempt.cost_usd,
            cost_status=attempt.cost_status,
            attempts=candidate.attempts,
        ),
        ToolIntentTurnResult(intent=intent),
    )


def successful_usage_evidence(
    *,
    context: UsageEvidenceContext,
    response: ModelResponse,
    decision: dict[str, Any],
    attempt_summary: Mapping[str, object] | None,
) -> ModelUsageEvidence:
    """把受控响应与可选真实-provider attempt 汇总投影为结算 evidence。"""

    return ModelUsageEvidence(
        usage_kind="model",
        tenant_id=context.tenant_id,
        provider=response.provider,
        model=response.model,
        input_tokens=(
            cast(int | None, attempt_summary["input_tokens"])
            if attempt_summary is not None
            else response.token_usage.get("input_tokens")
        ),
        output_tokens=(
            cast(int | None, attempt_summary["output_tokens"])
            if attempt_summary is not None
            else response.token_usage.get("output_tokens")
        ),
        cost_usd=(
            cast(float | None, attempt_summary["cost_usd"])
            if attempt_summary is not None
            else response.cost_usd
        ),
        cost_status=(
            cast(Any, attempt_summary["cost_status"])
            if attempt_summary is not None
            else response.cost_status
        ),
        latency_ms=response.latency_ms,
        decision=decision,
        run_id=context.run_id,
        agent_id=context.agent_id,
        request_id=context.request_id,
        trace_id=context.trace_id,
    )


__all__ = [
    "ModelApprovalRequired",
    "ModelLoopReservationError",
    "model_policy_approval_request",
    "normalize_tool_turn",
    "successful_usage_evidence",
    "validate_loop_reservation_bounds",
]
