"""以确定性规则分流工单，并保留可替换模型调用的证据链示例。"""

from __future__ import annotations

from typing import Literal, cast

from agent_harness.models import (
    BoundModelInvocationService,
    ModelRequest,
)
from agent_harness.runtime import (
    AgentExecutionContext,
    AgentExecutionRequest,
    AgentExecutionResult,
    ApprovalGrant,
)
from agents.examples._shared import publish_example_trace
from agents.examples.ticket_triage.schemas import TicketTriageInput, TicketTriageOutput

TicketCategory = Literal["access", "billing", "bug", "incident", "unknown"]
TicketPriority = Literal["low", "normal", "high", "urgent"]

_RULES: tuple[tuple[TicketCategory, tuple[str, ...], TicketPriority, str], ...] = (
    ("incident", ("outage", "down", "production unavailable", "sev1"), "urgent", "oncall"),
    ("bug", ("bug", "crash", "exception", "broken"), "high", "engineering"),
    ("billing", ("invoice", "billing", "charge", "refund"), "normal", "finance"),
    ("access", ("login", "permission", "access", "password"), "normal", "support"),
)


class TicketTriageExecutor:
    """先用可解释规则分类，再调用模型服务留下 Provider 接缝证据。

    分类结果只来自本地规则，保证模板在离线与回归测试中可重复；模型调用
    不参与决策，专门演示用量、审计和可替换 Provider 如何进入执行链路。
    """

    async def run(
        self,
        request: AgentExecutionRequest,
        context: AgentExecutionContext,
    ) -> AgentExecutionResult:
        """规范化输入、执行确定性分流，并写入可追溯的执行事件。

        ``prompt`` 是为交互入口保留的兼容字段，显式 ``text`` 始终优先。
        未命中规则时返回人工复核而不是猜测类别，以避免示例误导使用者把
        低置信度判断当成自动化处置结果。
        """
        payload = dict(request.input)
        prompt = payload.pop("prompt", None)
        payload["text"] = str(payload.get("text") or prompt or "")
        data = TicketTriageInput.model_validate(payload)
        category, priority, route, confidence = _classify(data.text)
        needs_review = category == "unknown"
        model = cast(
            BoundModelInvocationService,
            context.require_service("model_invocation"),
        )
        model_response = await model.complete(
            ModelRequest(
                provider="fake",
                prompt=f"classify ticket: {data.text}",
                estimated_input_tokens=max(1, len(data.text) // 4),
                max_output_tokens=32,
            ),
            operation_key="examples.ticket_triage:model-classification",
        )
        trace = await publish_example_trace(
            context=context,
            request=request,
            name="examples.ticket_triage.classified",
            payload={
                "category": category,
                "priority": priority,
                "route": route,
                "confidence": confidence,
                "needs_review": needs_review,
                "model": {
                    "provider": model_response.provider,
                    "model": model_response.model,
                },
            },
        )
        output = TicketTriageOutput(
            category=category,
            priority=priority,
            confidence=confidence,
            route=route,
            needs_review=needs_review,
            model_provider=model_response.provider,
            model_trace_ref=str(trace["trace_ref"]),
        )
        return AgentExecutionResult.completed(output.to_payload())

    async def resume(
        self,
        request: AgentExecutionRequest,
        context: AgentExecutionContext,
        grant: ApprovalGrant,
    ) -> AgentExecutionResult:
        """明确拒绝恢复，说明该示例没有审批型 continuation。"""
        del request, context, grant
        return AgentExecutionResult.failed("ticket triage has no approval continuation")


def _classify(text: str) -> tuple[TicketCategory, TicketPriority, str, float]:
    """按固定优先级匹配关键词，返回类别、优先级、路由和置信度。

    规则顺序具有业务含义：事故关键词必须先于通用 bug 等描述命中，避免
    生产不可用事件被降级路由。未命中时保留 ``unknown``，由调用方触发
    人工复核，而不是在此处引入不可解释的兜底分类。
    """
    normalized = text.casefold()
    for category, keywords, priority, route in _RULES:
        if any(keyword in normalized for keyword in keywords):
            return category, priority, route, 0.95
    return "unknown", "low", "manual-review", 0.2


executor = TicketTriageExecutor()
