"""确定性 ticket triage，同时调用 fake model 保留可替换 provider evidence。"""

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
    """关键词规则给出可解释分类，fake model 只作为 provider seam evidence。"""

    async def run(
        self,
        request: AgentExecutionRequest,
        context: AgentExecutionContext,
    ) -> AgentExecutionResult:
        payload = dict(request.input)
        payload.pop("source", None)
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
        del request, context, grant
        return AgentExecutionResult.failed("ticket triage has no approval continuation")


def _classify(text: str) -> tuple[TicketCategory, TicketPriority, str, float]:
    normalized = text.casefold()
    for category, keywords, priority, route in _RULES:
        if any(keyword in normalized for keyword in keywords):
            return category, priority, route, 0.95
    return "unknown", "low", "manual-review", 0.2


executor = TicketTriageExecutor()
