"""审批后工具执行的 grant 校验、跨进程仲裁与结果持久化。"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, cast
from uuid import uuid4

from sqlalchemy.exc import IntegrityError

from agent_harness.artifacts import FileArtifactStore
from agent_harness.audit import AuditService
from agent_harness.runtime.executor import ApprovalGrant
from agent_harness.security.redaction import redact_secrets
from agent_harness.storage import SQLAlchemyStorage, ToolInvocationCreate
from agent_harness.storage.evidence_repositories import (
    EvidenceOperationKind,
    operation_event_capacity,
)
from agent_harness.tools.approval_identity import hash_tool_arguments
from agent_harness.tools.approved_grant import (
    ApprovedToolExecutionUncertain,
    ApprovedToolGrantError,
    ApprovedToolLeaseLost,
    validate_approval_grant,
)
from agent_harness.tools.execution_support import (
    ApprovedModelToolExecution,
    error_result,
    invoke_handler,
    redact_tool_result,
    source_ref,
    validate_arguments,
)
from agent_harness.tools.output_guard import guarded_tool_payload
from agent_harness.tools.types import (
    BuiltinTool,
    ToolCallRequest,
    ToolCallResult,
    ToolErrorCode,
    ToolExecutionError,
    ToolRuntimeContext,
)

if TYPE_CHECKING:
    from agent_harness.events.model_tool_loop import (
        ModelToolLoopEventProducer,
        ModelToolLoopEventStep,
    )
    from agent_harness.models.tool_intent import ToolIntent
    from agent_harness.tools.types import ResolvedToolIntent


class ApprovedToolExecutor:
    """审批后工具的 at-most-once 执行器，与普通 policy 评估路径隔离。"""

    def __init__(
        self,
        *,
        tools: dict[str, BuiltinTool],
        storage: SQLAlchemyStorage | None,
        artifact_store: FileArtifactStore,
        audit: AuditService | None,
        inline_result_bytes: int,
        agent_tool_allowlist: set[str],
        enforce_agent_tool_allowlist: bool,
    ) -> None:
        """装配审批后执行所需的工具、持久化、artifact 与审计协作者。

        此执行器刻意不接受 policy engine：审批 grant 已是先前策略流程的持久化结果，
        重复评估可能因规则漂移推翻已确认操作，破坏 at-most-once 恢复语义。
        """

        self._tools = tools
        self._storage = storage
        self._artifact_store = artifact_store
        self._audit = audit
        self._inline_result_bytes = inline_result_bytes
        self._agent_tool_allowlist = agent_tool_allowlist
        self._enforce_agent_tool_allowlist = enforce_agent_tool_allowlist
        self._model_execution = (
            ApprovedModelToolExecution(
                storage=storage,
                artifact_store=artifact_store,
                call_handler=self._call_handler,
                record_audit=self._record_audit,
            )
            if storage is not None
            else None
        )

    async def execute(
        self,
        request: ToolCallRequest,
        *,
        context: ToolRuntimeContext,
        grant: ApprovalGrant,
        events: ModelToolLoopEventProducer | None = None,
        intent: ToolIntent | None = None,
        resolved: ResolvedToolIntent | None = None,
    ) -> ToolCallResult:
        """校验持久化 lease，并在唯一 claim 后至多执行一次 approved action。"""

        if self._storage is None:
            raise RuntimeError("approved tool execution requires storage")
        tool = self._tools.get(request.tool_name)
        if tool is None:
            raise ApprovedToolGrantError("approval grant references an unknown tool")
        validate_approval_grant(grant, request, context, tool)
        async with self._storage.uow() as uow:
            lease = await uow.approvals.get_resolution(grant.approval_id)
        if lease is None:
            raise ApprovedToolGrantError("approval grant does not match its persisted lease")
        if lease.lease_id != grant.lease_id:
            raise ApprovedToolLeaseLost(
                f"approval lease was fenced by another owner: {grant.approval_id}"
            )
        approval = lease.approval
        persisted = {
            "tenant_id": approval.tenant_id,
            "identity_id": str(approval.metadata.get("identity_id") or approval.requested_by),
            "session_id": str(approval.metadata.get("session_id") or ""),
            "agent_id": approval.agent_id,
            "run_id": approval.run_id,
            "action": approval.action,
            "resource": approval.resource,
            "arguments_hash": str(approval.metadata.get("arguments_hash") or ""),
        }
        grant_fields = {
            "tenant_id": grant.tenant_id,
            "identity_id": grant.identity_id,
            "session_id": grant.session_id,
            "agent_id": grant.agent_id,
            "run_id": grant.run_id,
            "action": grant.action,
            "resource": grant.resource,
            "arguments_hash": grant.arguments_hash,
        }
        mismatch = next(
            (field for field, value in persisted.items() if grant_fields[field] != value),
            None,
        )
        if mismatch is not None:
            raise ApprovedToolGrantError(f"approval grant persistence mismatch: {mismatch}")
        async with self._storage.uow() as uow:
            existing = await uow.tool_invocations.get_by_approval_id(grant.approval_id)
        if lease.state not in {"claimed", "execution_owned", "recovery_pending"}:
            if (
                existing
                and existing.result_ref
                and existing.execution_state in {"completed", "failed"}
            ):
                payload = self._artifact_store.read_json(existing.result_ref)
                replayed = ToolCallResult.model_validate(payload)
                if self._model_execution is None:  # pragma: no cover - storage已关闭失败
                    raise RuntimeError("approved model tool execution requires storage")
                await self._model_execution.replay_terminal_events(
                    replayed,
                    context=context,
                    grant=grant,
                    events=events,
                    intent=intent,
                    resolved=resolved,
                )
                return replayed
            if lease.state == "needs_review":
                raise ApprovedToolExecutionUncertain(
                    f"approved tool execution needs review: {grant.approval_id}"
                )
            raise ApprovedToolGrantError("approval grant lease is no longer executable")

        arguments_hash = hash_tool_arguments(request.arguments)
        if intent is not None and resolved is not None:
            if self._model_execution is None:  # pragma: no cover - storage已关闭失败
                raise RuntimeError("approved model tool execution requires storage")
            return await self._model_execution.execute(
                request=request,
                context=context,
                grant=grant,
                tool=tool,
                existing=existing,
                events=events,
                intent=intent,
                resolved=resolved,
            )
        args_payload = {"arguments": request.arguments}
        args_artifact = self._artifact_store.reference_json(args_payload)
        created = False
        try:
            async with self._storage.uow() as uow:
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
                reserved_event_count = await uow.event_capacity.reserve(
                    run_id=grant.run_id,
                    operation_kind=EvidenceOperationKind.TOOL_INVOCATION,
                )
                await uow.tool_invocations.create(
                    ToolInvocationCreate(
                        tenant_id=context.actor.tenant_id,
                        agent_id=context.agent_id,
                        run_id=context.run_id,
                        tool_name=request.tool_name,
                        args_ref=args_artifact.ref,
                        status="executing",
                        approval_id=grant.approval_id,
                        arguments_hash=arguments_hash,
                        execution_state="executing",
                        trace_id=context.trace_id or request.trace_id,
                        request_id=context.request_id or request.request_id,
                        metadata={
                            "lease_id": grant.lease_id,
                            "reserved_event_count": reserved_event_count,
                        },
                    )
                )
                await uow.commit()
                created = True
        except IntegrityError:
            # unique approval_id 是跨进程仲裁点；冲突后只允许读取确定性结果。
            created = False

        if not created:
            if existing is None:
                async with self._storage.uow() as uow:
                    existing = await uow.tool_invocations.get_by_approval_id(grant.approval_id)
            if existing is None:
                raise ApprovedToolExecutionUncertain(
                    "approved tool claim collided but cannot be read"
                )
            if existing.result_ref and existing.execution_state in {"completed", "failed"}:
                payload = self._artifact_store.read_json(existing.result_ref)
                return ToolCallResult.model_validate(payload)
            raise ApprovedToolExecutionUncertain(
                f"approved tool execution needs review: {grant.approval_id}"
            )

        materialized_args = self._artifact_store.write_json(args_payload)
        if materialized_args != args_artifact:
            raise RuntimeError("tool argument artifact does not match reserved content reference")
        approved_context = context.model_copy(deep=True).authorize_approved_call(grant.approval_id)
        event_step: ModelToolLoopEventStep | None = None
        if events is not None:
            if intent is None or resolved is None:
                raise ApprovedToolGrantError("approved tool event correlation is missing")
            event_step = await events.begin_tool(
                context=approved_context,
                intent=intent,
                resolved=resolved,
                capacity_pre_reserved=True,
            )
        result = await self._call_handler(request, context=approved_context, tool=tool)
        result_ref = self._artifact_store.write_json(result.to_payload()).ref
        execution_state = "completed" if result.status == "completed" else "failed"
        async with self._storage.uow() as uow:
            await uow.tool_invocations.finish_approved_claim(
                approval_id=grant.approval_id,
                result_ref=result_ref,
                execution_state=execution_state,
                status=result.status,
            )
            if events is None:
                await uow.event_capacity.settle(
                    run_id=grant.run_id,
                    reserved_event_count=operation_event_capacity(
                        EvidenceOperationKind.TOOL_INVOCATION
                    ),
                    consumed=0,
                )
            await uow.commit()
        if events is not None and event_step is not None:
            await events.finish_tool(step=event_step, result=result)
        return result

    async def _call_handler(
        self,
        request: ToolCallRequest,
        *,
        context: ToolRuntimeContext,
        tool: BuiltinTool,
        propagate_unknown: bool = False,
    ) -> ToolCallResult:
        """校验输入并执行 handler，不重新进入已通过的 approval policy gate。"""

        invocation_id = str(uuid4())
        result_source_ref = source_ref(request.tool_name, invocation_id, context.run_id)
        if not self._is_agent_tool_allowed(request.tool_name):
            return error_result(
                request,
                context,
                invocation_id,
                result_source_ref,
                ToolErrorCode.ALLOWLIST_DENIED,
                f"tool is not allowlisted for agent: {request.tool_name}",
            )
        validation_error = validate_arguments(tool.input_schema, request.arguments)
        if validation_error is not None:
            return error_result(
                request,
                context,
                invocation_id,
                result_source_ref,
                ToolErrorCode.SCHEMA_VALIDATION_FAILED,
                validation_error.message,
                field_path=validation_error.field_path,
                hint=validation_error.hint,
            )
        if tool.preflight is not None:
            try:
                preflight_result = invoke_handler(tool.preflight, request, context)
                if inspect.isawaitable(preflight_result):
                    preflight_result = await preflight_result
            except ToolExecutionError as exc:
                preflight_result = error_result(
                    request,
                    context,
                    invocation_id,
                    result_source_ref,
                    exc.code,
                    exc.message,
                    field_path=exc.field_path,
                    hint=exc.hint,
                )
            if preflight_result is not None:
                result = cast(ToolCallResult, preflight_result)
                await self._record_audit(
                    context,
                    request.tool_name,
                    result.invocation_id,
                    result.status,
                )
                return result
        try:
            raw_result = invoke_handler(tool.handler, request, context)
            if inspect.isawaitable(raw_result):
                raw_result = await raw_result
            if isinstance(raw_result, ToolCallResult):
                result = raw_result
            else:
                payload, artifact_ref, truncation = guarded_tool_payload(
                    tool_name=request.tool_name,
                    invocation_id=invocation_id,
                    payload=raw_result,
                    artifact_store=self._artifact_store,
                    inline_bytes=self._inline_result_bytes,
                )
                result = ToolCallResult(
                    tool_name=request.tool_name,
                    status="completed",
                    invocation_id=invocation_id,
                    result=payload,
                    source_ref=result_source_ref,
                    artifact_ref=artifact_ref,
                    truncation=truncation,
                    request_id=context.request_id or request.request_id,
                    trace_id=context.trace_id or request.trace_id,
                )
        except ToolExecutionError as exc:
            result = error_result(
                request,
                context,
                invocation_id,
                result_source_ref,
                exc.code,
                exc.message,
                field_path=exc.field_path,
                hint=exc.hint,
            )
        except Exception as exc:  # noqa: BLE001 - legacy入口仍投影稳定脱敏失败
            if propagate_unknown:
                raise
            result = error_result(
                request,
                context,
                invocation_id,
                result_source_ref,
                ToolErrorCode.EXECUTION_FAILED,
                str(redact_secrets(str(exc))),
            )
        result = redact_tool_result(result)
        await self._record_audit(context, request.tool_name, result.invocation_id, result.status)
        return result

    async def _record_audit(
        self,
        context: ToolRuntimeContext,
        tool_name: str,
        invocation_id: str,
        status: str,
    ) -> None:
        """在审计服务存在时记录审批后副作用的最小身份和状态，不阻塞恢复主路径。"""

        if self._audit is None:
            return
        await self._audit.record(
            actor=context.actor,
            action="tool.invocation",
            resource=f"tool:{tool_name}",
            payload={
                "tool_name": tool_name,
                "invocation_id": invocation_id,
                "status": status,
                "run_id": context.run_id,
                "tenant_id": context.actor.tenant_id,
                "user_id": context.actor.user_id,
                "agent_id": context.agent_id,
                "request_id": context.request_id,
                "trace_id": context.trace_id,
            },
        )

    def _is_agent_tool_allowed(self, tool_name: str) -> bool:
        """仅在显式启用时应用工具白名单，保持历史配置的兼容行为。"""

        if not self._enforce_agent_tool_allowlist:
            return True
        return tool_name in self._agent_tool_allowlist
