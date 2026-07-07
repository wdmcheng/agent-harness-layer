"""Agent Harness 跨边界 payload 的公共契约。"""

from agent_harness.contracts.dto import HarnessDTO as HarnessDTO
from agent_harness.contracts.errors import ApiErrorEnvelope as ApiErrorEnvelope
from agent_harness.contracts.errors import ErrorDetail as ErrorDetail
from agent_harness.contracts.errors import HarnessError as HarnessError
from agent_harness.contracts.trust import (
    ContextInput as ContextInput,
)
from agent_harness.contracts.trust import (
    ContextOutput as ContextOutput,
)
from agent_harness.contracts.trust import (
    ContextRef as ContextRef,
)
from agent_harness.contracts.trust import (
    GuardrailDecision as GuardrailDecision,
)
from agent_harness.contracts.trust import (
    GuardrailDecisionStatus as GuardrailDecisionStatus,
)
from agent_harness.contracts.trust import (
    PolicyDecision as PolicyDecision,
)
from agent_harness.contracts.trust import (
    SourceRef as SourceRef,
)
from agent_harness.contracts.trust import (
    TrustLevel as TrustLevel,
)

_DTO_EXPORTS = ["HarnessDTO"]

_ERROR_EXPORTS = [
    "ApiErrorEnvelope",
    "ErrorDetail",
    "HarnessError",
]

_TRUST_EXPORTS = [
    "ContextInput",
    "ContextOutput",
    "ContextRef",
    "GuardrailDecision",
    "GuardrailDecisionStatus",
    "PolicyDecision",
    "SourceRef",
    "TrustLevel",
]

__all__ = [  # pyright: ignore[reportUnsupportedDunderAll]
    *_DTO_EXPORTS,
    *_ERROR_EXPORTS,
    *_TRUST_EXPORTS,
]
