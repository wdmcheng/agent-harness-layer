"""Guardrail/context event payload helpers."""

from __future__ import annotations

from agent_harness.contracts import GuardrailDecision, SourceRef, TrustLevel
from agent_harness.security.redaction import redact_secrets


def guardrail_event_payload(
    *,
    decision: GuardrailDecision,
    source_ref: SourceRef,
    trust_level: TrustLevel,
    summary: str,
    truncated: bool = False,
) -> dict[str, object]:
    return {
        # summary 是最容易被调用方误塞原始 provider/tool 输出的字段，必须和
        # decision metadata 一样在进入 event/evidence 前脱敏。
        "summary": redact_secrets(summary),
        "source_ref": source_ref.to_payload(),
        "trust_level": trust_level.value,
        "truncated": truncated,
        "decision": redact_secrets(decision.to_payload()),
    }
