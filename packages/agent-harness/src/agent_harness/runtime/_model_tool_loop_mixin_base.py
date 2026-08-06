"""模型工具循环私有协作者共享的运行状态与精确方法签名。"""
# pyright: reportPrivateUsage=false, reportUnusedClass=false, reportUnusedFunction=false

from __future__ import annotations

from collections.abc import MutableMapping
from datetime import datetime
from typing import Any, Literal

from agent_harness.artifacts import FileArtifactStore
from agent_harness.events.model_tool_loop import ModelToolLoopEventProducer
from agent_harness.identity import IdentityContext
from agent_harness.models.providers import ModelRequest, ModelResponse
from agent_harness.models.tool_catalog import (
    ToolCatalog,
    ToolCatalogEntry,
    ToolCatalogSelection,
)
from agent_harness.models.tool_intent import (
    ToolIntent,
)
from agent_harness.models.usage import UsageEvidenceContext
from agent_harness.registry.descriptor import AgentModelPolicy, AgentModelToolLoop
from agent_harness.runtime._model_tool_loop_contracts import (
    LoopStepObserver,
    ModelToolLoopApprovalSnapshot,
    ModelToolLoopLimitOverrides,
    ModelToolLoopLimitState,
    MonotonicClock,
    ToolCatalogResolver,
    ToolRegistryResolver,
    TrustedClock,
    _ContextAssemblyRuntime,
    _ModelToolLoopApprovalStore,
    _ModelToolLoopFinal,
    _ModelToolLoopRestore,
    _ModelToolTurnRuntime,
)
from agent_harness.runtime.executor import AgentApprovalRequest, ApprovalGrant
from agent_harness.storage import ModelToolLoopRecord, SQLAlchemyStorage
from agent_harness.tools.types import ToolRuntimeContext


class _ModelToolLoopMixinBase:
    """只声明同一 bound service 的状态，不创建第二运行时或第二状态机。"""

    _model_turns: _ModelToolTurnRuntime
    _tool_catalog_resolver: ToolCatalogResolver
    _tool_registry_resolver: ToolRegistryResolver
    _context_assembly: _ContextAssemblyRuntime
    _context: UsageEvidenceContext
    _identity: IdentityContext
    _loop_limits: AgentModelToolLoop | None
    _model_policy: AgentModelPolicy
    _step_observer: LoopStepObserver | None
    _approval_store: _ModelToolLoopApprovalStore | None
    _loop_events: ModelToolLoopEventProducer | None
    _storage: SQLAlchemyStorage | None
    _artifact_store: FileArtifactStore | None
    _trusted_clock: TrustedClock
    _monotonic_clock: MonotonicClock
    _monotonic_deadlines: MutableMapping[tuple[str, str, str, datetime, datetime], float]

    async def run(
        self,
        request: ModelRequest,
        *,
        operation_key: str,
        tool_selection: ToolCatalogSelection | None = None,
        limits: ModelToolLoopLimitOverrides | None = None,
    ) -> ModelResponse:
        raise NotImplementedError()

    async def resume(
        self, request: ModelRequest, *, operation_key: str, grant: ApprovalGrant
    ) -> ModelResponse:
        raise NotImplementedError()

    async def _continue(
        self,
        *,
        initial_request: ModelRequest,
        current_request: ModelRequest,
        operation_key: str,
        catalog: ToolCatalog,
        loop_id: str,
        start_turn_ordinal: int,
        limit_state: ModelToolLoopLimitState,
        settled_turn_input_state: ModelToolLoopLimitState | None = None,
    ) -> _ModelToolLoopFinal:
        raise NotImplementedError()

    async def _continue_with_terminal(
        self,
        *,
        initial_request: ModelRequest,
        current_request: ModelRequest,
        operation_key: str,
        catalog: ToolCatalog,
        loop_id: str,
        start_turn_ordinal: int,
        limit_state: ModelToolLoopLimitState,
        settled_turn_input_state: ModelToolLoopLimitState | None = None,
    ) -> ModelResponse:
        raise NotImplementedError()

    async def _request_after_tool_result(
        self,
        current_request: ModelRequest,
        *,
        tool_result: object,
        intent: ToolIntent,
        expected_tool_name: str,
        limit_state: ModelToolLoopLimitState,
    ) -> tuple[ModelRequest, str]:
        raise NotImplementedError()

    def _freeze_limits(
        self, overrides: ModelToolLoopLimitOverrides | None
    ) -> ModelToolLoopLimitState:
        raise NotImplementedError()

    def _check_deadline(self, state: ModelToolLoopLimitState) -> None:
        raise NotImplementedError()

    @staticmethod
    def _check_model_budget_remaining(state: ModelToolLoopLimitState) -> None:
        raise NotImplementedError()

    def _check_tool_can_continue(
        self, state: ModelToolLoopLimitState, *, turn_ordinal: int
    ) -> None:
        raise NotImplementedError()

    async def _account_turn_usage(
        self, state: ModelToolLoopLimitState, *, usage_call_id: str, loop_id: str, turn_ordinal: int
    ) -> ModelToolLoopLimitState:
        raise NotImplementedError()

    def _loop_id(self, operation_key: str) -> str:
        raise NotImplementedError()

    def _tool_context(self) -> ToolRuntimeContext:
        raise NotImplementedError()

    async def _ensure_durable_loop(
        self,
        *,
        initial_request: ModelRequest,
        operation_key: str,
        catalog: ToolCatalog,
        loop_id: str,
        limit_state: ModelToolLoopLimitState,
    ) -> ModelToolLoopRecord | None:
        raise NotImplementedError()

    async def _wait_durable_loop(
        self,
        loop_id: str,
        *,
        approval: AgentApprovalRequest,
        snapshot: ModelToolLoopApprovalSnapshot,
    ) -> None:
        raise NotImplementedError()

    async def _resume_durable_loop(self, loop_id: str, *, approval_id: str) -> None:
        raise NotImplementedError()

    async def _commit_durable_turn(
        self,
        *,
        loop_id: str,
        turn_ordinal: int,
        limit_state: ModelToolLoopLimitState,
        next_request: ModelRequest,
        model_usage_call_id: str,
        tool_call_id: str,
        approval_id: str | None,
        checkpoint_ref: str | None,
        context_ref: str,
    ) -> None:
        raise NotImplementedError()

    async def _settle_durable_model_turn(
        self,
        *,
        loop_id: str,
        turn_ordinal: int,
        usage_call_id: str,
        limit_state: ModelToolLoopLimitState,
    ) -> None:
        raise NotImplementedError()

    async def _expire_durable_model_turn(
        self,
        *,
        loop_id: str,
        turn_ordinal: int,
        usage_call_id: str,
        limit_state: ModelToolLoopLimitState,
    ) -> None:
        raise NotImplementedError()

    async def _complete_durable_loop(
        self, loop_id: str, *, operation_key: str, final: _ModelToolLoopFinal
    ) -> None:
        raise NotImplementedError()

    async def _validate_terminal_prerequisites(
        self, uow: Any, *, record: ModelToolLoopRecord, operation_key: str
    ) -> None:
        raise NotImplementedError()

    async def _validate_owner_prerequisites(
        self,
        uow: Any,
        *,
        record: ModelToolLoopRecord,
        operation_key: str,
        usage_turn_ordinals: range,
        allow_terminal_approval: bool,
        allow_pending_current_turn: bool,
    ) -> None:
        raise NotImplementedError()

    def _replay_completed_response(
        self, record: ModelToolLoopRecord, *, initial_request: ModelRequest
    ) -> ModelResponse:
        raise NotImplementedError()

    async def _restore_active_loop(
        self, record: ModelToolLoopRecord, *, initial_request: ModelRequest, operation_key: str
    ) -> _ModelToolLoopRestore:
        raise NotImplementedError()

    async def _fail_durable_loop(
        self, loop_id: str, *, status: Literal["failed", "needs_review"], code: str
    ) -> None:
        raise NotImplementedError()

    async def _durable_loop(self, loop_id: str) -> ModelToolLoopRecord:
        raise NotImplementedError()

    def _approval_snapshot_matches(
        self,
        snapshot: ModelToolLoopApprovalSnapshot,
        *,
        request: ModelRequest,
        operation_key: str,
        grant: ApprovalGrant,
    ) -> bool:
        raise NotImplementedError()

    def _observe(self, step: str) -> None:
        raise NotImplementedError()

    def _request_matches_agent_policy(self, request: ModelRequest) -> bool:
        raise NotImplementedError()

    @staticmethod
    def _catalog_entry(intent: ToolIntent, *, catalog: ToolCatalog) -> ToolCatalogEntry:
        raise NotImplementedError()

    @staticmethod
    def _resolved_matches_intent(resolved: object, *, intent: ToolIntent, entry: object) -> bool:
        raise NotImplementedError()

    @staticmethod
    def _response_matches_request(response: ModelResponse, request: ModelRequest) -> bool:
        raise NotImplementedError()
