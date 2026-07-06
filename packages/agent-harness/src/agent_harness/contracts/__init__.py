"""Agent Harness 跨边界 payload 的公共契约。"""

from agent_harness.contracts.dto import HarnessDTO
from agent_harness.contracts.errors import ApiErrorEnvelope, ErrorDetail, HarnessError
from agent_harness.contracts.trust import (
    ContextInput,
    ContextOutput,
    ContextRef,
    GuardrailDecision,
    GuardrailDecisionStatus,
    PolicyDecision,
    SourceRef,
    TrustLevel,
)

__all__ = [
    "ApiErrorEnvelope",
    "ContextInput",
    "ContextOutput",
    "ContextRef",
    "ErrorDetail",
    "GuardrailDecision",
    "GuardrailDecisionStatus",
    "HarnessDTO",
    "HarnessError",
    "PolicyDecision",
    "SourceRef",
    "TrustLevel",
]
