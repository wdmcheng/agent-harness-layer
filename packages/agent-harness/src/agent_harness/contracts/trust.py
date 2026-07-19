"""信任级别、上下文引用和决策值的边界契约。"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Self

from pydantic import Field

from agent_harness.contracts.dto import HarnessDTO


def _context_refs() -> list[ContextRef]:
    """为每个 DTO 实例提供独立的空引用列表，避免可变默认值跨请求共享。"""

    return []


class TrustLevel(StrEnum):
    """内容进入模型上下文前必须携带的可信级别。"""

    TRUSTED = "trusted"
    INTERNAL = "internal"
    USER = "user"
    UNTRUSTED = "untrusted"


class GuardrailDecisionStatus(StrEnum):
    """policy 和 guardrail 共用的显式决策词汇。"""

    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


class SourceRef(HarnessDTO):
    """指向内容来源的引用，不承载原始大内容。"""

    kind: str
    uri: str
    label: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ContextRef(HarnessDTO):
    """内容跨越上下文边界时必须保留的来源、信任和截断元数据。"""

    context_id: str
    source_ref: SourceRef
    trust_level: TrustLevel
    truncated: bool = False
    token_count: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ContextInput(HarnessDTO):
    """进入 ContextAssembler 前的内容和来源/信任元数据。"""

    content: str
    refs: list[ContextRef] = Field(default_factory=_context_refs)
    trust_level: TrustLevel = TrustLevel.UNTRUSTED
    metadata: dict[str, Any] = Field(default_factory=dict)


class ContextOutput(HarnessDTO):
    """ContextAssembler 或 provider normalization 后输出的内容。"""

    content: str
    refs: list[ContextRef] = Field(default_factory=_context_refs)
    trust_level: TrustLevel
    truncated: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class GuardrailDecision(HarnessDTO):
    """guardrail 与 policy 共享的可序列化决策。"""

    status: GuardrailDecisionStatus
    reason: str
    metadata: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def allow(cls, reason: str = "allowed", metadata: dict[str, Any] | None = None) -> Self:
        """构造允许决策，并复制默认空元数据以保持可序列化结构一致。"""

        return cls(
            status=GuardrailDecisionStatus.ALLOW,
            reason=reason,
            metadata=metadata or {},
        )

    @classmethod
    def deny(cls, reason: str, metadata: dict[str, Any] | None = None) -> Self:
        """构造拒绝决策；调用方提供的原因进入审计与 API 统一错误映射。"""

        return cls(
            status=GuardrailDecisionStatus.DENY,
            reason=reason,
            metadata=metadata or {},
        )

    @classmethod
    def require_approval(cls, reason: str, metadata: dict[str, Any] | None = None) -> Self:
        """构造需要人工审批的中间决策，不将其误归类为直接拒绝。"""

        return cls(
            status=GuardrailDecisionStatus.REQUIRE_APPROVAL,
            reason=reason,
            metadata=metadata or {},
        )


PolicyDecision = GuardrailDecision
