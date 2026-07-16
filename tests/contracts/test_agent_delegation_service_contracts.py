"""真实 DelegationService 的授权、重放与 local child 状态机合同。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, cast

import pytest
from sqlalchemy import select, update
from tests.contracts.agent_delegation_service_runtime_test_support import (
    _build_service as _build_service,
)
from tests.contracts.agent_delegation_service_runtime_test_support import (
    _descriptor as _descriptor,
)
from tests.contracts.agent_delegation_service_runtime_test_support import (
    _identity as _identity,
)
from tests.contracts.agent_delegation_service_runtime_test_support import (
    _InlineChildRuntime as _InlineChildRuntime,
)
from tests.contracts.agent_delegation_service_runtime_test_support import (
    _ParentDetailOrchestrator as _ParentDetailOrchestrator,
)
from tests.contracts.agent_delegation_service_runtime_test_support import (
    _Policy as _Policy,
)
from tests.contracts.agent_delegation_service_runtime_test_support import (
    _UsageProvider as _UsageProvider,
)

from agent_harness.contracts import GuardrailDecisionStatus
from agent_harness.delegation import (
    DelegationRequest,
    delegation_request_hash,
)
from agent_harness.delegation.service import DelegationError, DelegationMode, DelegationService
from agent_harness.events import (
    CanonicalEvent,
    CanonicalEventType,
    EventBus,
    EventSink,
    LocalJsonlEventSink,
    PostgreSQLEventSink,
)
from agent_harness.events.serialization import canonical_event_bytes
from agent_harness.identity import IdentityContext
from agent_harness.models import (
    ModelDecision,
    ModelInvocationService,
    ModelProviderInvocationError,
    ModelRequest,
    ModelResponse,
    ModelRouter,
    ModelRouterConfig,
    UsageEvidenceContext,
)
from agent_harness.policy import PolicyCheck, PolicyEvaluation
from agent_harness.registry import (
    AgentBudget,
    AgentDescriptor,
    AgentModelPolicy,
    AgentRegistry,
    AgentToolPolicy,
)
from agent_harness.runtime import RunDetailResult, RunResult, RunStatus
from agent_harness.storage import SQLAlchemyStorage, run_migrations
from agent_harness.storage.delegation_models import (
    AgentDelegationModel,
    DelegationAggregateModel,
    DelegationBudgetReservationModel,
)
from agent_harness.storage.delegation_repositories import DelegationClaimCreate
from agent_harness.storage.event_capacity_repositories import (
    MAX_EVENT_SEQ,
    EvidenceOperationKind,
)
from agent_harness.storage.evidence_repositories import EvidenceOutboxRepository
from agent_harness.storage.models import AgentRunModel, RunEvidenceOutboxModel, SessionModel
from agent_harness.storage.repositories import RunCreate, SessionCreate
from agent_harness.storage.run_trace_gate import StorageRunTraceResolver
from app.api.routes.runs import get_run_with_orchestrator


async def _record_usage(
    *,
    storage: SQLAlchemyStorage,
    service: DelegationService,
    run_id: str,
    agent_id: str,
    usage_call_id: str,
    provider: _UsageProvider,
    expect_failure: bool = False,
) -> None:
    usage_service = ModelInvocationService(
        router=ModelRouter(
            config=ModelRouterConfig(default_model="fake-basic"),
            providers={"fake": provider},
        ),
        storage=storage,
        event_bus=cast(Any, service)._event_bus,
    )
    invocation = usage_service.complete(
        ModelRequest(provider="fake", prompt="durable usage", max_output_tokens=2),
        context=UsageEvidenceContext(
            tenant_id="tenant-a",
            run_id=run_id,
            agent_id=agent_id,
            request_id="request-a",
            trace_id="trace-parent",
        ),
        usage_call_id=usage_call_id,
    )
    if expect_failure:
        with pytest.raises(ModelProviderInvocationError):
            await invocation
    else:
        await invocation


def _request(parent_run_id: str, **updates: object) -> DelegationRequest:
    payload: dict[str, object] = {
        "parent_run_id": parent_run_id,
        "source_agent_id": "agent-source",
        "target_agent_id": "agent-target",
        "child_input": {"prompt": "delegate safely"},
        "idempotency_key": "delegation-key",
        "request_id": "request-a",
    }
    payload.update(updates)
    return DelegationRequest.model_validate(payload)


__all__ = [
    "AgentBudget",
    "AgentDelegationModel",
    "AgentDescriptor",
    "AgentModelPolicy",
    "AgentRegistry",
    "AgentRunModel",
    "AgentToolPolicy",
    "Any",
    "CanonicalEvent",
    "CanonicalEventType",
    "DelegationAggregateModel",
    "DelegationBudgetReservationModel",
    "DelegationClaimCreate",
    "DelegationError",
    "DelegationMode",
    "DelegationRequest",
    "DelegationService",
    "EventBus",
    "EventSink",
    "EvidenceOperationKind",
    "EvidenceOutboxRepository",
    "GuardrailDecisionStatus",
    "IdentityContext",
    "Literal",
    "LocalJsonlEventSink",
    "MAX_EVENT_SEQ",
    "ModelDecision",
    "ModelInvocationService",
    "ModelProviderInvocationError",
    "ModelRequest",
    "ModelResponse",
    "ModelRouter",
    "ModelRouterConfig",
    "Path",
    "PolicyCheck",
    "PolicyEvaluation",
    "PostgreSQLEventSink",
    "RunCreate",
    "RunDetailResult",
    "RunEvidenceOutboxModel",
    "RunResult",
    "RunStatus",
    "SQLAlchemyStorage",
    "SessionCreate",
    "SessionModel",
    "StorageRunTraceResolver",
    "UsageEvidenceContext",
    "_InlineChildRuntime",
    "_ParentDetailOrchestrator",
    "_Policy",
    "_UsageProvider",
    "_build_service",
    "_descriptor",
    "_identity",
    "_record_usage",
    "_request",
    "canonical_event_bytes",
    "cast",
    "delegation_request_hash",
    "get_run_with_orchestrator",
    "pytest",
    "run_migrations",
    "select",
    "update",
]
