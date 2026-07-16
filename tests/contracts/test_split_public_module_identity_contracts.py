"""职责拆分保持公开对象模块身份的兼容合同。"""

from typing import Literal

from agent_harness.delegation.service import (
    DelegationError,
    DelegationExecutionResult,
    DelegationMode,
)
from agent_harness.registry.registry import RegistryLoadError
from agent_harness.scaffold import (
    ExecutorRollbackInventory,
    ScaffoldError,
    ScaffoldResult,
    discover_agents_dir,
)
from agent_harness.storage.delegation_repositories import (
    DelegatedChildRunRecord,
    DelegationAggregateRecord,
    DelegationBudgetExceeded,
    DelegationBudgetReservationRecord,
    DelegationClaimCreate,
    DelegationClaimResult,
    DelegationRecord,
    DelegationRecoveryCandidate,
    DelegationStorageConflict,
    DelegationStorageError,
    DelegationSummaryProjectionRecord,
    DelegationUsageEvidenceRecord,
)
from agent_harness.storage.evidence_repositories import UsageSettlementClaim
from app.api.routes.runs import (
    AgentRunCreateRequest,
    RunCreateRequest,
    RunCreateResponse,
    RunDetailResponse,
    RunEventsResponse,
    RunResumeRequest,
    error_responses,
    get_agent_registry,
    get_delegation_service,
    get_event_sink,
    get_run_orchestrator,
    public_events,
    request_id_from,
)


def test_split_public_objects_keep_facade_module_identity() -> None:
    """私有职责模块不得泄漏到公开对象的文档、序列化与诊断身份。"""

    # typing 会缓存相同参数的 Literal；facade 只能重导出，不能改写全局别名身份。
    assert DelegationMode is Literal["local", "service"]
    assert DelegationMode.__module__ == "typing"

    expected_groups = {
        "agent_harness.scaffold": (
            ExecutorRollbackInventory,
            ScaffoldError,
            ScaffoldResult,
            discover_agents_dir,
        ),
        "agent_harness.registry.registry": (RegistryLoadError,),
        "agent_harness.delegation.service": (
            DelegationError,
            DelegationExecutionResult,
        ),
        "agent_harness.storage.delegation_repositories": (
            DelegatedChildRunRecord,
            DelegationAggregateRecord,
            DelegationBudgetExceeded,
            DelegationBudgetReservationRecord,
            DelegationClaimCreate,
            DelegationClaimResult,
            DelegationRecord,
            DelegationRecoveryCandidate,
            DelegationStorageConflict,
            DelegationStorageError,
            DelegationSummaryProjectionRecord,
            DelegationUsageEvidenceRecord,
        ),
        "agent_harness.storage.evidence_repositories": (UsageSettlementClaim,),
        "app.api.routes.runs": (
            AgentRunCreateRequest,
            RunCreateRequest,
            RunCreateResponse,
            RunDetailResponse,
            RunEventsResponse,
            RunResumeRequest,
            error_responses,
            get_agent_registry,
            get_delegation_service,
            get_event_sink,
            get_run_orchestrator,
            public_events,
            request_id_from,
        ),
    }
    for expected_module, public_objects in expected_groups.items():
        assert {item.__module__ for item in public_objects} == {expected_module}
