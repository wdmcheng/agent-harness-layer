"""模型工具循环的稳定 DTO、协议与纯转换。"""
# pyright: reportUnusedClass=false, reportUnusedFunction=false

from __future__ import annotations

import json
import math
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Protocol, cast

from pydantic import (
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from agent_harness.context import ContextAssemblyResult, ContextFragment
from agent_harness.contracts.dto import HarnessDTO
from agent_harness.events.model_tool_loop import ModelToolLoopEventProducer
from agent_harness.identity import IdentityContext
from agent_harness.models.providers import ModelRequest, ModelResponse
from agent_harness.models.structured import structured_digest
from agent_harness.models.tool_catalog import (
    ToolCatalog,
    ToolCatalogSelection,
    provider_tool_catalog_bytes,
)
from agent_harness.models.tool_intent import (
    ToolIntent,
)
from agent_harness.models.usage import (
    UsageEvidenceContext,
)
from agent_harness.registry.descriptor import AgentModelPolicy, AgentModelToolLoop
from agent_harness.runtime.executor import AgentApprovalRequest, ApprovalGrant
from agent_harness.tools.execution_support import redact_tool_result
from agent_harness.tools.types import (
    ResolvedToolIntent,
    ToolCallResult,
    ToolErrorCode,
    ToolRuntimeContext,
    tool_status_for_error,
)


class ModelToolLoopError(RuntimeError):
    """工具循环在安全边界关闭时只暴露稳定错误码。"""

    def __init__(self, code: str) -> None:
        """拒绝把 prompt、arguments、tool output 或内部异常写入公开消息。"""

        super().__init__(code)
        self.code = code


class ModelToolLoopLimitOverrides(HarnessDTO):
    """调用方只能逐项缩小的五字段 exact nullable DTO。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)

    max_turns: int | None = Field(ge=1, le=64, strict=True)
    max_total_tokens: int | None = Field(ge=1, strict=True)
    max_total_cost_usd: float | None
    max_tool_output_bytes: int | None = Field(ge=1, le=1_048_576, strict=True)
    max_duration_seconds: int | None = Field(ge=1, le=3_600, strict=True)

    @field_validator("max_total_cost_usd", mode="before")
    @classmethod
    def validate_cost(cls, value: object) -> object:
        """null表示继承；非null只接受有限非负number且拒绝bool。"""

        if value is None:
            return value
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not math.isfinite(value)
            or value < 0
        ):
            raise ValueError("max_total_cost_usd must be finite and non-negative")
        return value


class ModelToolLoopLimitState(HarnessDTO):
    """审批与后续回合共用的冻结上限、绝对deadline和累计实际usage。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)

    max_turns: int = Field(ge=1, le=64, strict=True)
    max_total_tokens: int = Field(ge=1, strict=True)
    max_total_cost_usd: float | None
    max_tool_output_bytes: int = Field(ge=1, le=1_048_576, strict=True)
    max_duration_seconds: int = Field(ge=1, le=3_600, strict=True)
    loop_started_at: datetime
    deadline_at: datetime
    total_tokens_used: int = Field(ge=0, strict=True)
    total_cost_usd: float | None

    @field_validator("max_total_cost_usd", "total_cost_usd", mode="before")
    @classmethod
    def validate_costs(cls, value: object) -> object:
        """快照中的成本边界与累计量只能是null或有限非负number。"""

        if value is None:
            return value
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not math.isfinite(value)
            or value < 0
        ):
            raise ValueError("model tool loop cost values must be finite and non-negative")
        return value

    @model_validator(mode="after")
    def validate_state(self) -> ModelToolLoopLimitState:
        """冻结时间、累计量与effective maxima必须始终组成同一有效快照。"""

        for value in (self.loop_started_at, self.deadline_at):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("model tool loop timestamps must be timezone-aware")
        if self.deadline_at != self.loop_started_at + timedelta(seconds=self.max_duration_seconds):
            raise ValueError("model tool loop deadline does not match frozen duration")
        if self.total_tokens_used > self.max_total_tokens:
            raise ValueError("model tool loop token usage exceeds maximum")
        if self.max_total_cost_usd is not None:
            if self.total_cost_usd is None or self.total_cost_usd > self.max_total_cost_usd:
                raise ValueError("model tool loop cost usage exceeds maximum")
        if self.total_cost_usd is not None and (
            not math.isfinite(self.total_cost_usd) or self.total_cost_usd < 0
        ):
            raise ValueError("model tool loop cost usage must be finite and non-negative")
        return self


class ModelToolLoopApprovalSnapshot(HarnessDTO):
    """审批等待时冻结的data-only循环位置与完整绑定。

    该DTO只进入受控artifact/checkpoint，不进入公开事件；digest覆盖模型请求、
    intent、catalog、运行身份和operation，恢复时不得从当前配置重新解释。
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)

    snapshot_digest: str
    operation_key: str
    initial_request: ModelRequest
    current_request: ModelRequest
    context: UsageEvidenceContext
    identity_id: str
    session_id: str
    intent: ToolIntent
    catalog: ToolCatalog
    action: str
    resource: str
    limits: ModelToolLoopLimitState

    @model_validator(mode="after")
    def validate_snapshot_digest(self) -> ModelToolLoopApprovalSnapshot:
        """逐值复算snapshot identity，拒绝artifact或内存对象后置篡改。"""

        if self.snapshot_digest != _approval_snapshot_digest(
            operation_key=self.operation_key,
            initial_request=self.initial_request,
            current_request=self.current_request,
            context=self.context,
            identity_id=self.identity_id,
            session_id=self.session_id,
            intent=self.intent,
            catalog=self.catalog,
            action=self.action,
            resource=self.resource,
            limits=self.limits,
        ):
            raise ValueError("model tool loop approval snapshot digest mismatch")
        return self


class ModelToolLoopApprovalRequired(ModelToolLoopError):
    """把已冻结的审批请求交给既有Agent executor waiting协议。"""

    def __init__(
        self,
        approval: AgentApprovalRequest,
        *,
        snapshot: ModelToolLoopApprovalSnapshot,
    ) -> None:
        super().__init__(ToolErrorCode.APPROVAL_REQUIRED.value)
        self.approval = approval
        self.snapshot = snapshot.model_copy(deep=True)


class _ModelToolLoopFinal(HarnessDTO):
    """把最终响应与该回合已结算usage绑定到唯一terminal提交。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    response: ModelResponse
    turn_ordinal: int = Field(ge=1, strict=True)
    usage_call_id: str
    limit_state: ModelToolLoopLimitState


class _ModelToolLoopRestore(HarnessDTO):
    """恢复入口同时携带耐久累计与当前已结算轮的原始预约视图。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    current_request: ModelRequest
    start_turn_ordinal: int = Field(ge=1, strict=True)
    limit_state: ModelToolLoopLimitState
    settled_turn_input_state: ModelToolLoopLimitState | None


def _approval_snapshot_digest(
    *,
    operation_key: str,
    initial_request: ModelRequest,
    current_request: ModelRequest,
    context: UsageEvidenceContext,
    identity_id: str,
    session_id: str,
    intent: ToolIntent,
    catalog: ToolCatalog,
    action: str,
    resource: str,
    limits: ModelToolLoopLimitState,
) -> str:
    """从全部授权与续跑事实计算稳定snapshot digest。"""

    return structured_digest(
        {
            "schema_version": "model-tool-loop-approval-snapshot-v1",
            "operation_key": operation_key,
            "initial_request": initial_request.to_payload(),
            "current_request": current_request.to_payload(),
            "context": context.to_payload(),
            "identity_id": identity_id,
            "session_id": session_id,
            "intent": intent.to_payload(),
            "catalog": catalog.to_payload(),
            "provider_tool_catalog_json": provider_tool_catalog_bytes(catalog).decode("utf-8"),
            "action": action,
            "resource": resource,
            "limits": limits.to_payload(),
        }
    )


def _approval_snapshot(
    *,
    operation_key: str,
    initial_request: ModelRequest,
    current_request: ModelRequest,
    context: UsageEvidenceContext,
    identity_id: str,
    session_id: str,
    intent: ToolIntent,
    catalog: ToolCatalog,
    action: str,
    resource: str,
    limits: ModelToolLoopLimitState,
) -> ModelToolLoopApprovalSnapshot:
    """构造自校验的冻结审批快照，避免调用方自行填写digest。"""

    return ModelToolLoopApprovalSnapshot(
        snapshot_digest=_approval_snapshot_digest(
            operation_key=operation_key,
            initial_request=initial_request,
            current_request=current_request,
            context=context,
            identity_id=identity_id,
            session_id=session_id,
            intent=intent,
            catalog=catalog,
            action=action,
            resource=resource,
            limits=limits,
        ),
        operation_key=operation_key,
        initial_request=initial_request,
        current_request=current_request,
        context=context,
        identity_id=identity_id,
        session_id=session_id,
        intent=intent,
        catalog=catalog,
        action=action,
        resource=resource,
        limits=limits,
    )


class _ModelToolTurnRuntime(Protocol):
    """循环 owner 可调用的内部单轮模型结算接缝。"""

    async def complete_tool_loop_turn(
        self,
        request: ModelRequest,
        *,
        context: UsageEvidenceContext,
        usage_call_id: str,
        loop_id: str,
        turn_ordinal: int,
        operation_identity_digest: str,
        tool_catalog: ToolCatalog,
        actor: IdentityContext,
        loop_token_bound: int,
        loop_cost_bound: float | None,
    ) -> object: ...

    async def read_tool_loop_turn_usage(
        self,
        *,
        context: UsageEvidenceContext,
        usage_call_id: str,
        loop_id: str,
        turn_ordinal: int,
    ) -> object: ...


class _ResolvedToolRegistry(Protocol):
    """循环只依赖 Registry 的公开 resolve/call，不读取私有 handler。"""

    def resolve_intent(
        self,
        intent: ToolIntent,
        *,
        catalog: ToolCatalog,
    ) -> ResolvedToolIntent: ...

    async def call(
        self,
        request: ResolvedToolIntent,
        *,
        context: ToolRuntimeContext,
        intent: ToolIntent,
        catalog: ToolCatalog,
        events: ModelToolLoopEventProducer | None = None,
    ) -> ToolCallResult: ...


class _ContextAssemblyRuntime(Protocol):
    """工具结果只能通过公共 ContextAssembler service 进入下一轮。"""

    async def assemble(
        self,
        *,
        tenant_id: str,
        run_id: str | None,
        fragments: list[ContextFragment],
        token_budget: int,
        loop_id: str | None = None,
        turn_ordinal: int | None = None,
        tool_call_id: str | None = None,
    ) -> ContextAssemblyResult: ...


class _ModelToolLoopApprovalStore(Protocol):
    """循环只依赖的审批snapshot持久化/解析能力。"""

    def create(
        self,
        *,
        snapshot: ModelToolLoopApprovalSnapshot,
        reason: str,
    ) -> AgentApprovalRequest: ...

    async def resolve(
        self,
        *,
        grant: ApprovalGrant,
    ) -> ModelToolLoopApprovalSnapshot: ...


ToolCatalogResolver = Callable[[str, ToolCatalogSelection | None], ToolCatalog]
ToolRegistryResolver = Callable[[str, str], _ResolvedToolRegistry]
LoopLimitsResolver = Callable[[str], AgentModelToolLoop | None]
LoopStepObserver = Callable[[str], None]
AgentModelPolicyResolver = Callable[[str], AgentModelPolicy]
TrustedClock = Callable[[], datetime]
MonotonicClock = Callable[[], float]


def _tool_result_fragment(result: ToolCallResult) -> ContextFragment:
    """把 Registry 已守卫的结果投影为最小 untrusted context fragment。

    该转换只读取公开 `ToolCallResult`，不会回读 handler 原始值或 artifact 正文。
    更细的 truncation/injection trace 由后续专用结果投影合同继续收窄。
    """

    try:
        content = json.dumps(
            result.result or {},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError):
        raise ModelToolLoopError("tool.result_invalid") from None
    token_estimate = max(1, (len(content.encode("utf-8")) + 3) // 4)
    return ContextFragment(
        source_ref=result.source_ref,
        trust_level="untrusted",
        content=content,
        token_estimate=token_estimate,
        kind="tool_result",
        artifact_ref=result.artifact_ref,
        truncation=dict(result.truncation),
        injection_summary=list(
            cast(list[str], result.truncation.get("prompt_injection_signals", []))
        ),
    )


def _guard_tool_result(
    result: object,
    *,
    expected_tool_name: str,
) -> ToolCallResult:
    """深拷贝、脱敏并验证Registry结果的最小判别联合。

    Registry 是唯一执行入口，但循环仍在跨层边界防御性重验DTO，避免测试替身、
    自定义Registry或后置篡改把trusted/raw结果直接送入模型上下文。
    """

    if type(result) is not ToolCallResult:
        raise ModelToolLoopError("tool.result_invalid")
    try:
        snapshot = ToolCallResult.model_validate(
            ToolCallResult.model_dump(result, mode="python")
        ).model_copy(deep=True)
        guarded = redact_tool_result(snapshot)
    except (AttributeError, TypeError, ValueError):
        raise ModelToolLoopError("tool.result_invalid") from None
    if (
        guarded.tool_name != expected_tool_name
        or guarded.trust_level != "untrusted"
        or not guarded.invocation_id
        or not guarded.source_ref
    ):
        raise ModelToolLoopError("tool.result_invalid")
    truncated = guarded.truncation.get("truncated")
    injection_signals = guarded.truncation.get("prompt_injection_signals", [])
    if type(truncated) is not bool or type(injection_signals) is not list:
        raise ModelToolLoopError("tool.result_invalid")
    if any(type(item) is not str for item in cast(list[object], injection_signals)):
        raise ModelToolLoopError("tool.result_invalid")
    if guarded.status == "completed":
        if guarded.result is None or guarded.error is not None:
            raise ModelToolLoopError("tool.result_invalid")
        if truncated and (
            guarded.artifact_ref is None or guarded.result != {"artifact_ref": guarded.artifact_ref}
        ):
            raise ModelToolLoopError("tool.result_invalid")
        return guarded
    if (
        guarded.result is not None
        or guarded.error is None
        or guarded.status != tool_status_for_error(guarded.error.code)
    ):
        raise ModelToolLoopError("tool.result_invalid")
    return guarded


def _next_turn_request(
    request: ModelRequest,
    *,
    assembly: ContextAssemblyResult,
) -> ModelRequest:
    """只把冻结 assembly 与显式不可信边界组成下一轮 user 输入。"""

    retained = assembly.retained_fragments
    if (
        not assembly.input_refs
        or assembly.trust_summary != {"untrusted": len(assembly.input_refs)}
        or any(fragment.trust_level != "untrusted" for fragment in retained)
        or assembly.assembled_text != "\n".join(fragment.content for fragment in retained)
    ):
        raise ModelToolLoopError("tool.result_invalid")
    injection_summary = [
        signal for fragment in retained for signal in (fragment.injection_summary or [])
    ]

    prompt = json.dumps(
        {
            "schema_version": "model-tool-loop-next-turn-v1",
            "original_prompt": request.prompt,
            "context_assembly": {
                "output_ref": assembly.output_ref,
                "input_refs": assembly.input_refs,
                "trust_level": "untrusted",
                "trust_summary": assembly.trust_summary,
                "injection_summary": injection_summary,
                "assembled_text": assembly.assembled_text,
            },
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return request.model_copy(update={"prompt": prompt})
