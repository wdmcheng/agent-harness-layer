"""工具输入、结果清洗与审批后模型调用生命周期的共享支撑。"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable
from typing import TYPE_CHECKING, Any, Protocol, cast

from agent_harness.artifacts import FileArtifactStore
from agent_harness.runtime.executor import ApprovalGrant
from agent_harness.security.redaction import redact_secrets
from agent_harness.storage import SQLAlchemyStorage
from agent_harness.storage.adapters.sqlalchemy import SQLAlchemyUnitOfWork
from agent_harness.storage.evidence_repositories import (
    EvidenceOperationKind,
    operation_event_capacity,
)
from agent_harness.tools.approved_grant import (
    ApprovedToolExecutionUncertain,
    ApprovedToolGrantError,
    ApprovedToolLeaseLost,
)
from agent_harness.tools.durable_execution import (
    ModelToolExecutionClaimService,
    ModelToolExecutionNeedsReview,
    ToolExecutionPermit,
    build_model_tool_invocation_claim,
    model_tool_execution_lock,
)
from agent_harness.tools.types import (
    BuiltinTool,
    ToolCallRequest,
    ToolCallResult,
    ToolError,
    ToolErrorCode,
    ToolRuntimeContext,
    tool_status_for_error,
)

if TYPE_CHECKING:
    from agent_harness.events.model_tool_loop import (
        ModelToolLoopEventProducer,
        ModelToolLoopEventStep,
    )
    from agent_harness.models.tool_intent import ToolIntent
    from agent_harness.storage.tool_repositories import ToolInvocationRecord
    from agent_harness.tools.types import ResolvedToolIntent


class ArgumentValidationError:
    """JSON Schema 子集校验失败的进程内结果。"""

    def __init__(self, message: str, *, field_path: str | None, hint: str | None = None) -> None:
        """保存输入校验失败的公开诊断；此对象不抛异常，供 Registry 统一映射。"""

        self.message = message
        self.field_path = field_path
        self.hint = hint


def validate_arguments(
    schema: dict[str, Any], arguments: dict[str, Any]
) -> ArgumentValidationError | None:
    """校验内置工具当前承诺支持的 JSON Schema 子集。"""

    if schema.get("type", "object") != "object":
        return ArgumentValidationError("tool input schema must be an object", field_path=None)

    required = schema.get("required", [])
    if isinstance(required, list):
        required_fields = cast(list[object], required)
        for field in required_fields:
            if isinstance(field, str) and field not in arguments:
                return ArgumentValidationError(
                    f"missing required argument: {field}",
                    field_path=field,
                    hint="provide all required tool arguments",
                )

    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        return None
    property_specs = cast(dict[object, object], properties)
    for field, spec in property_specs.items():
        if field not in arguments or not isinstance(field, str) or not isinstance(spec, dict):
            continue
        typed_spec = cast(dict[str, Any], spec)
        expected_type = typed_spec.get("type")
        if expected_type is None:
            continue
        if not matches_json_schema_type(arguments[field], expected_type):
            return ArgumentValidationError(
                f"argument {field} must be {expected_type}",
                field_path=field,
                hint="match the tool input schema",
            )
    return None


def matches_json_schema_type(value: Any, expected_type: object) -> bool:
    """判断运行时值是否匹配工具输入 schema 的基础类型。"""

    if expected_type == "string":
        return isinstance(value, str)
    if expected_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if expected_type == "boolean":
        return isinstance(value, bool)
    if expected_type == "object":
        return isinstance(value, dict)
    if expected_type == "array":
        return isinstance(value, list)
    return True


def error_result(
    request: ToolCallRequest,
    context: ToolRuntimeContext,
    invocation_id: str,
    source_ref: str,
    code: ToolErrorCode,
    message: str,
    *,
    field_path: str | None = None,
    hint: str | None = None,
    policy: dict[str, Any] | None = None,
) -> ToolCallResult:
    """把工具边界错误转换为稳定、可关联的结果 DTO。"""

    return ToolCallResult(
        tool_name=request.tool_name,
        status=tool_status_for_error(code),
        invocation_id=invocation_id,
        error=ToolError(code=code, message=message, field_path=field_path, hint=hint),
        source_ref=source_ref,
        policy=policy or {},
        request_id=context.request_id or request.request_id,
        trace_id=context.trace_id or request.trace_id,
    )


def source_ref(tool_name: str, invocation_id: str, run_id: str | None) -> str:
    """生成一次工具调用的稳定来源引用。"""

    run_part = run_id or "adhoc"
    return f"tool://{tool_name}/{run_part}/{invocation_id}"


def invoke_handler(handler: Any, request: ToolCallRequest, context: ToolRuntimeContext) -> Any:
    """兼容纯参数 handler 和需要完整 request/context 的内置工具。"""

    signature = inspect.signature(handler)
    if "context" in signature.parameters:
        return handler(request, context=context)
    return handler(request.arguments)


def redact_tool_result(result: ToolCallResult) -> ToolCallResult:
    """在返回、持久化和 trace 前统一清理 handler 结果中的 secret。"""

    return ToolCallResult.model_validate(redact_secrets(result.to_payload()))


class ApprovedHandlerCall(Protocol):
    """主执行器提供的已守卫handler调用边界。"""

    def __call__(
        self,
        request: ToolCallRequest,
        *,
        context: ToolRuntimeContext,
        tool: BuiltinTool,
        propagate_unknown: bool = False,
    ) -> Awaitable[ToolCallResult]: ...


class ApprovedAuditCall(Protocol):
    """主执行器提供的最小审计写入边界。"""

    def __call__(
        self,
        context: ToolRuntimeContext,
        tool_name: str,
        invocation_id: str,
        status: str,
    ) -> Awaitable[None]: ...


class ApprovedModelToolExecution:
    """只负责模型工具的耐久claim、事件恢复与结果终态。"""

    def __init__(
        self,
        *,
        storage: SQLAlchemyStorage,
        artifact_store: FileArtifactStore,
        call_handler: ApprovedHandlerCall,
        record_audit: ApprovedAuditCall,
    ) -> None:
        self._storage = storage
        self._artifact_store = artifact_store
        self._call_handler = call_handler
        self._record_audit = record_audit

    async def replay_terminal_events(
        self,
        result: ToolCallResult,
        *,
        context: ToolRuntimeContext,
        grant: ApprovalGrant,
        events: ModelToolLoopEventProducer | None,
        intent: ToolIntent | None,
        resolved: ResolvedToolIntent | None,
    ) -> None:
        """为approved exact result幂等补投稳定工具事件，不重新进入handler。"""

        if events is None:
            return
        if intent is None or resolved is None:
            raise ApprovedToolGrantError("approved tool event correlation is missing")
        approved_context = context.model_copy(deep=True).authorize_approved_call(grant.approval_id)
        event_step = await events.prepare_tool_claim(
            context=approved_context,
            intent=intent,
        )
        await events.start_tool(step=event_step, resolved=resolved)
        await events.finish_tool(step=event_step, result=result)

    async def execute(
        self,
        *,
        request: ToolCallRequest,
        context: ToolRuntimeContext,
        grant: ApprovalGrant,
        tool: BuiltinTool,
        existing: ToolInvocationRecord | None,
        events: ModelToolLoopEventProducer | None,
        intent: ToolIntent,
        resolved: ResolvedToolIntent,
    ) -> ToolCallResult:
        """按tool-call与approval双identity串行化公开approved执行及exact replay。"""

        async with model_tool_execution_lock(
            self._storage,
            tenant_id=context.actor.tenant_id,
            tool_call_id=resolved.tool_call_id,
            approval_id=grant.approval_id,
        ):
            return await self._execute_owned(
                request=request,
                context=context,
                grant=grant,
                tool=tool,
                existing=existing,
                events=events,
                intent=intent,
                resolved=resolved,
            )

    async def _execute_owned(
        self,
        *,
        request: ToolCallRequest,
        context: ToolRuntimeContext,
        grant: ApprovalGrant,
        tool: BuiltinTool,
        existing: ToolInvocationRecord | None,
        events: ModelToolLoopEventProducer | None,
        intent: ToolIntent,
        resolved: ResolvedToolIntent,
    ) -> ToolCallResult:
        """在公共execution锁内复用tool_call_id claim、permit与exact结果。"""

        args_payload = {"arguments": request.arguments}
        args_artifact = self._artifact_store.reference_json(args_payload)
        approved_context = context.model_copy(deep=True).authorize_approved_call(grant.approval_id)
        event_step: ModelToolLoopEventStep | None = None
        if events is not None:
            event_step = await events.prepare_tool_claim(
                context=approved_context,
                intent=intent,
            )
        expected_capacity = operation_event_capacity(EvidenceOperationKind.TOOL_INVOCATION)
        claim_data = build_model_tool_invocation_claim(
            resolved=resolved,
            context=approved_context,
            args_ref=args_artifact.ref,
            approval_id=grant.approval_id,
            metadata=(
                existing.metadata
                if existing is not None
                else {"reserved_event_count": expected_capacity}
            ),
        )
        if existing is not None:
            claim_data = claim_data.model_copy(
                update={"execution_fence": (existing.execution_fence or 0) + 1}
            )

        async def prepare_approved_owner_uow(uow: SQLAlchemyUnitOfWork) -> None:
            """同一owner提交审批fence、最大容量、事件identity与首次claim。"""

            fenced = await uow.approvals.fence_resolution_lease(
                approval_id=grant.approval_id,
                run_id=grant.run_id,
                tenant_id=grant.tenant_id,
                lease_id=grant.lease_id,
            )
            if not fenced:
                raise ApprovedToolLeaseLost(
                    f"approval lease is no longer active: {grant.approval_id}"
                )
            if events is not None and event_step is not None:
                await events.reserve_tool_in_owner_uow(step=event_step, uow=uow)
                return
            reserved = await uow.event_capacity.reserve(
                run_id=grant.run_id,
                operation_kind=EvidenceOperationKind.TOOL_INVOCATION,
            )
            if reserved != expected_capacity:
                raise RuntimeError("approved model tool event capacity is inconsistent")

        claim_service = ModelToolExecutionClaimService(self._storage)
        claimed = await claim_service.acquire(
            claim_data,
            prepare_new_owner_uow=prepare_approved_owner_uow,
        )
        if not isinstance(claimed, ToolExecutionPermit):
            if claimed.result_ref is None:
                raise ApprovedToolExecutionUncertain(
                    f"approved model tool result is missing: {grant.approval_id}"
                )
            replayed = ToolCallResult.model_validate(
                self._artifact_store.read_json(claimed.result_ref)
            )
            await self.replay_terminal_events(
                replayed,
                context=context,
                grant=grant,
                events=events,
                intent=intent,
                resolved=resolved,
            )
            return replayed
        materialized_args = self._artifact_store.write_json(args_payload)
        if materialized_args != args_artifact:
            raise RuntimeError("tool argument artifact does not match claimed reference")
        if events is not None and event_step is not None:
            await events.start_tool(step=event_step, resolved=resolved)
        await claim_service.require_handler_permit(claimed)
        try:
            result = await self._call_handler(
                request,
                context=approved_context,
                tool=tool,
                propagate_unknown=True,
            )
        except Exception:
            await claim_service.mark_handler_outcome_unknown(claimed)
            await self._record_audit(
                approved_context,
                request.tool_name,
                claimed.invocation_id,
                "needs_review",
            )
            raise ApprovedToolExecutionUncertain(
                f"approved model tool handler outcome is unknown: {grant.approval_id}"
            ) from None
        except BaseException:
            # 取消或进程级中断不能抹掉已消费permit；先围栏再保留原异常。
            await claim_service.mark_handler_outcome_unknown(claimed)
            await self._record_audit(
                approved_context,
                request.tool_name,
                claimed.invocation_id,
                "needs_review",
            )
            raise
        try:
            result_ref = self._artifact_store.write_json(result.to_payload()).ref
        except Exception:
            await claim_service.mark_handler_outcome_unknown(
                claimed,
                reason="result_evidence_missing",
            )
            await self._record_audit(
                approved_context,
                request.tool_name,
                claimed.invocation_id,
                "needs_review",
            )
            raise ApprovedToolExecutionUncertain(
                f"approved model tool result evidence is unknown: {grant.approval_id}"
            ) from None
        except BaseException:
            await claim_service.mark_handler_outcome_unknown(
                claimed,
                reason="result_evidence_missing",
            )
            await self._record_audit(
                approved_context,
                request.tool_name,
                claimed.invocation_id,
                "needs_review",
            )
            raise
        try:
            await claim_service.complete(
                claimed,
                result_ref=result_ref,
                execution_state="completed" if result.status == "completed" else "failed",
                status=result.status,
            )
        except ModelToolExecutionNeedsReview:
            await self._record_audit(
                approved_context,
                request.tool_name,
                claimed.invocation_id,
                "needs_review",
            )
            raise ApprovedToolExecutionUncertain(
                f"approved model tool result commit is unknown: {grant.approval_id}"
            ) from None
        if events is not None and event_step is not None:
            await events.finish_tool(step=event_step, result=result)
        return result


__all__ = [
    "ApprovedModelToolExecution",
    "ArgumentValidationError",
    "error_result",
    "invoke_handler",
    "matches_json_schema_type",
    "redact_tool_result",
    "source_ref",
    "validate_arguments",
]
