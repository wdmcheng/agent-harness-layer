"""职责拆分保持公开对象模块身份与薄 façade 的兼容合同。"""

import ast
import inspect
import textwrap
from typing import get_args

from agent_harness.delegation._service_types import DelegationMode as PrivateDelegationMode
from agent_harness.delegation.service import (
    DelegationError,
    DelegationExecutionResult,
    DelegationMode,
)
from agent_harness.models._invocation_evidence import ModelInvocationEvidenceMixin
from agent_harness.models._invocation_execution import ModelInvocationExecutionMixin
from agent_harness.models._invocation_settlement import _ModelSettlementMixin
from agent_harness.models._settlement_publication import SettlementPublicationMixin
from agent_harness.registry.registry import RegistryLoadError
from agent_harness.runtime.shared_budget import SharedBudgetRuntime
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

    # facade 必须重导出私有职责模块创建的同一别名；不能依赖 CPython 对等价
    # Literal 的进程级缓存身份，该缓存并非跨 Python patch 版本的公开合同。
    assert DelegationMode is PrivateDelegationMode
    assert get_args(DelegationMode) == ("local", "service")
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


def test_model_invocation_split_has_one_linear_evidence_owner() -> None:
    """拆分后的 settlement 不得靠多继承顺序覆盖同名占位方法。"""

    assert ModelInvocationExecutionMixin.__bases__ == (_ModelSettlementMixin,)
    assert SettlementPublicationMixin.__bases__ == (ModelInvocationEvidenceMixin,)
    assert "_durable_response" not in SettlementPublicationMixin.__dict__
    for name in ("_final_event_id", "_safe_decision", "_durable_response"):
        assert getattr(ModelInvocationExecutionMixin, name) is getattr(
            ModelInvocationEvidenceMixin, name
        )


def test_shared_budget_runtime_is_thin_public_facade() -> None:
    """公开 runtime 只编排稳定入口，变化中的快照、身份与恢复逻辑必须私有隔离。"""

    for method_name in (
        "ledger_create",
        "operation_identity",
        "delegation_identity",
        "delegation_replay_identity",
        "model_router_config",
        "embedding_price_config",
    ):
        source = textwrap.dedent(inspect.getsource(getattr(SharedBudgetRuntime, method_name)))
        function = ast.parse(source).body[0]
        assert isinstance(function, ast.FunctionDef)
        executable = [
            item
            for item in function.body
            if not (
                isinstance(item, ast.Expr)
                and isinstance(item.value, ast.Constant)
                and isinstance(item.value.value, str)
            )
        ]
        assert len(executable) == 1 and isinstance(executable[0], ast.Return), (
            f"{method_name} 仍在公开 façade 中持有变化逻辑"
        )
