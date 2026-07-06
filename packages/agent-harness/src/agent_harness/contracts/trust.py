"""Trust, context, and decision boundary contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Self

from pydantic import Field

from agent_harness.contracts.dto import HarnessDTO


def _context_refs() -> list[ContextRef]:
    return []


class TrustLevel(StrEnum):
    """Trust level for content entering model context."""

    TRUSTED = "trusted"
    INTERNAL = "internal"
    USER = "user"
    UNTRUSTED = "untrusted"


class GuardrailDecisionStatus(StrEnum):
    """Shared decision vocabulary for policy and guardrail checks."""

    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


class SourceRef(HarnessDTO):
    """Reference to the source that produced a context item."""

    kind: str
    uri: str
    label: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ContextRef(HarnessDTO):
    """Reference metadata retained with content crossing model boundaries."""

    context_id: str
    source_ref: SourceRef
    trust_level: TrustLevel
    truncated: bool = False
    token_count: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ContextInput(HarnessDTO):
    """Content prepared for context assembly."""

    content: str
    refs: list[ContextRef] = Field(default_factory=_context_refs)
    trust_level: TrustLevel = TrustLevel.UNTRUSTED
    metadata: dict[str, Any] = Field(default_factory=dict)


class ContextOutput(HarnessDTO):
    """Content emitted by context assembly or a provider boundary."""

    content: str
    refs: list[ContextRef] = Field(default_factory=_context_refs)
    trust_level: TrustLevel
    truncated: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class GuardrailDecision(HarnessDTO):
    """Serializable decision returned by guardrails or policy checks."""

    status: GuardrailDecisionStatus
    reason: str
    metadata: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def allow(cls, reason: str = "allowed", metadata: dict[str, Any] | None = None) -> Self:
        return cls(
            status=GuardrailDecisionStatus.ALLOW,
            reason=reason,
            metadata=metadata or {},
        )

    @classmethod
    def deny(cls, reason: str, metadata: dict[str, Any] | None = None) -> Self:
        return cls(
            status=GuardrailDecisionStatus.DENY,
            reason=reason,
            metadata=metadata or {},
        )

    @classmethod
    def require_approval(cls, reason: str, metadata: dict[str, Any] | None = None) -> Self:
        return cls(
            status=GuardrailDecisionStatus.REQUIRE_APPROVAL,
            reason=reason,
            metadata=metadata or {},
        )


PolicyDecision = GuardrailDecision
