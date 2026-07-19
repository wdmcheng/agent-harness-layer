"""Guardrail 与 context event payload 的稳定 DTO。"""

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
    """构造可公开持久化的 guardrail 事件 payload，并在边界前统一脱敏。

    ``summary`` 与 decision metadata 都可能携带不可信输入或上游输出，不能因
    调用方已做过处理就跳过这里的最后一道证据边界。
    """

    return {
        # summary 是最容易被调用方误塞原始 provider/tool 输出的字段，必须和
        # decision metadata 一样在进入 event/evidence 前脱敏。
        "summary": redact_secrets(summary),
        "source_ref": source_ref.to_payload(),
        "trust_level": trust_level.value,
        "truncated": truncated,
        "decision": redact_secrets(decision.to_payload()),
    }
