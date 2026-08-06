"""ToolRegistry 未批准调用的策略、claim、handler 与结果协调。"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Protocol, cast
from uuid import uuid4

from agent_harness.artifacts import FileArtifactStore
from agent_harness.contracts.trust import GuardrailDecisionStatus
from agent_harness.models.tool_catalog import (
    ToolCatalog,
)
from agent_harness.models.tool_intent import ToolIntent
from agent_harness.policy import PolicyCheck, PolicyEngine
from agent_harness.security.redaction import redact_secrets
from agent_harness.storage import SQLAlchemyStorage
from agent_harness.tools.durable_execution import (
    ModelToolExecutionClaimService,
    ModelToolExecutionNeedsReview,
    ModelToolExecutionReviewReason,
    ToolExecutionPermit,
    build_model_tool_invocation_claim,
)
from agent_harness.tools.execution_support import (
    error_result,
    invoke_handler,
    redact_tool_result,
    source_ref,
    validate_arguments,
)
from agent_harness.tools.output_guard import guarded_tool_payload
from agent_harness.tools.types import (
    BuiltinTool,
    ResolvedToolIntent,
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
    from agent_harness.storage.adapters.sqlalchemy import SQLAlchemyUnitOfWork


class RegistryExecutionRequest(Protocol):
    """执行协作者对 Registry TOCTOU 重验入口的精确依赖。"""

    def __call__(
        self,
        request: ToolCallRequest | ResolvedToolIntent,
        *,
        context: ToolRuntimeContext,
        intent: ToolIntent | None,
        catalog: ToolCatalog | None,
    ) -> ToolCallRequest: ...


async def call_unapproved_tool(
    request: ToolCallRequest | ResolvedToolIntent,
    *,
    context: ToolRuntimeContext,
    intent: ToolIntent | None,
    catalog: ToolCatalog | None,
    events: ModelToolLoopEventProducer | None,
    tools: dict[str, BuiltinTool],
    policy_engine: PolicyEngine,
    storage: SQLAlchemyStorage | None,
    artifact_store: FileArtifactStore,
    inline_result_bytes: int,
    is_agent_tool_allowed: Callable[[str], bool],
    execution_request: RegistryExecutionRequest,
    record_audit: Callable[[ToolRuntimeContext, str, str, str], Awaitable[None]],
) -> ToolCallResult:
    """执行一次未批准工具调用，并统一处理校验、策略、脱敏、审计与大结果外置。

    各个拒绝分支也写审计记录，保证调用尝试可追溯。工具 handler 的预检、策略
    与异常映射均发生在实际副作用前；已是 ``ToolCallResult`` 的适配器结果仍会
    经过脱敏，防止 provider 绕过公共输出边界。
    """

    resolved_request = request if type(request) is ResolvedToolIntent else None
    if events is not None and (intent is None or resolved_request is None):
        raise ValueError("model tool events require a resolved intent")
    request = execution_request(
        request,
        context=context,
        intent=intent,
        catalog=catalog,
    )
    invocation_id = str(uuid4())
    result_source_ref = source_ref(request.tool_name, invocation_id, context.run_id)
    tool = tools.get(request.tool_name)
    if tool is None:
        result = error_result(
            request,
            context,
            invocation_id,
            result_source_ref,
            ToolErrorCode.NOT_FOUND,
            f"tool not found: {request.tool_name}",
        )
        await record_audit(context, request.tool_name, invocation_id, result.status)
        return result
    if not is_agent_tool_allowed(request.tool_name):
        result = error_result(
            request,
            context,
            invocation_id,
            result_source_ref,
            ToolErrorCode.POLICY_DENIED,
            f"tool is not allowlisted for agent: {request.tool_name}",
        )
        await record_audit(context, request.tool_name, invocation_id, result.status)
        return result

    validation_error = validate_arguments(tool.input_schema, request.arguments)
    if validation_error is not None:
        result = error_result(
            request,
            context,
            invocation_id,
            result_source_ref,
            ToolErrorCode.SCHEMA_VALIDATION_FAILED,
            validation_error.message,
            field_path=validation_error.field_path,
            hint=validation_error.hint,
        )
        await record_audit(context, request.tool_name, invocation_id, result.status)
        return result

    policy = await policy_engine.evaluate(
        PolicyCheck(
            actor=context.actor,
            action=tool.action,
            resource=tool.resource,
            context={
                "tool_name": request.tool_name,
                "agent_id": request.agent_id,
                "run_id": context.run_id,
                "tenant_id": context.actor.tenant_id,
                "user_id": context.actor.user_id,
                "request_id": context.request_id or request.request_id,
                "trace_id": context.trace_id or request.trace_id,
            },
        )
    )
    policy_payload = policy.to_payload()
    if policy.decision == GuardrailDecisionStatus.DENY.value:
        result = error_result(
            request,
            context,
            invocation_id,
            result_source_ref,
            ToolErrorCode.POLICY_DENIED,
            policy.reason,
            policy=policy_payload,
        )
        await record_audit(context, request.tool_name, invocation_id, result.status)
        return result
    if policy.decision == GuardrailDecisionStatus.REQUIRE_APPROVAL.value:
        result = error_result(
            request,
            context,
            invocation_id,
            result_source_ref,
            ToolErrorCode.APPROVAL_REQUIRED,
            policy.reason,
            policy=policy_payload,
        )
        await record_audit(context, request.tool_name, invocation_id, result.status)
        return result

    event_step: ModelToolLoopEventStep | None = None
    if events is not None and intent is not None and resolved_request is not None:
        # 这里只完成纯 identity 构造和本地前缀对账；真正的容量/outbox 预约由
        # claim service 在首次 owner UoW 内提交，避免 reservation→claim 崩溃窗。
        event_step = await events.prepare_tool_claim(
            context=context,
            intent=intent,
        )

    claim_service: ModelToolExecutionClaimService | None = None
    execution_permit: ToolExecutionPermit | None = None
    if resolved_request is not None:
        if storage is None:
            raise RuntimeError("model tool execution requires durable storage")
        args_payload = {"arguments": request.arguments}
        args_artifact = artifact_store.reference_json(args_payload)
        claim_service = ModelToolExecutionClaimService(storage)
        claim_data = build_model_tool_invocation_claim(
            resolved=resolved_request,
            context=context,
            args_ref=args_artifact.ref,
            approval_id=None,
        )
        # Registry是普通模型工具claim的唯一fence分配者。读取只是候选快照；
        # repository中的CAS仍决定过期claim换租赢家，因此并发恢复不会双执行。
        async with storage.uow() as uow:
            existing_claim = await uow.tool_invocations.get_by_tool_call_id(
                resolved_request.tool_call_id
            )
        if existing_claim is not None:
            claim_data = claim_data.model_copy(
                update={"execution_fence": (existing_claim.execution_fence or 0) + 1}
            )

        async def prepare_event_reservation(uow: SQLAlchemyUnitOfWork) -> None:
            """把 claim 与工具事件最大容量/identity 绑定到同一 owner 提交。"""

            if events is None or event_step is None:
                raise RuntimeError("model tool event reservation is not configured")
            await events.reserve_tool_in_owner_uow(step=event_step, uow=uow)

        claimed = await claim_service.acquire(
            claim_data,
            prepare_new_owner_uow=(prepare_event_reservation if event_step is not None else None),
        )
        if not isinstance(claimed, ToolExecutionPermit):
            if claimed.result_ref is None:
                raise RuntimeError("terminal model tool claim is missing result")
            replayed = ToolCallResult.model_validate(artifact_store.read_json(claimed.result_ref))
            if events is not None and event_step is not None:
                await events.start_tool(step=event_step, resolved=resolved_request)
                await events.finish_tool(step=event_step, result=replayed)
            return replayed
        execution_permit = claimed
        materialized_args = artifact_store.write_json(args_payload)
        if materialized_args != args_artifact:
            raise RuntimeError("tool argument artifact does not match claimed reference")

    if events is not None and event_step is not None and resolved_request is not None:
        await events.start_tool(step=event_step, resolved=resolved_request)

    async def finish_event(result: ToolCallResult) -> ToolCallResult:
        """handler结果先耐久封存并经过公共守卫，再发布唯一工具终态事件。"""

        if claim_service is not None and execution_permit is not None:
            try:
                result_ref = artifact_store.write_json(result.to_payload()).ref
            except Exception:
                await fence_handler_outcome_unknown(reason="result_evidence_missing")
                raise ModelToolExecutionNeedsReview from None
            except BaseException:
                await fence_handler_outcome_unknown(reason="result_evidence_missing")
                raise
            await claim_service.complete(
                execution_permit,
                result_ref=result_ref,
                execution_state=("completed" if result.status == "completed" else "failed"),
                status=result.status,
            )
        if events is not None and event_step is not None:
            await events.finish_tool(step=event_step, result=result)
        return result

    if claim_service is not None and execution_permit is not None:
        await claim_service.require_handler_permit(execution_permit)

    async def fence_handler_outcome_unknown(
        *,
        reason: ModelToolExecutionReviewReason = "handler_outcome_unknown",
    ) -> None:
        """只为已消费的模型工具permit围栏未知结果；legacy调用保持既有映射。"""

        if claim_service is None or execution_permit is None:
            return
        await claim_service.mark_handler_outcome_unknown(
            execution_permit,
            reason=reason,
        )
        await record_audit(context, request.tool_name, invocation_id, "needs_review")

    # preflight 属于工具执行边界：必须在 Policy 允许且 durable permit 到手后运行。
    # 它可能访问运行环境，因此不能在 deny/waiting 路径产生副作用，也不能脱离
    # invocation claim 被 crash recovery 重放。
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
        except Exception:
            if claim_service is not None and execution_permit is not None:
                await fence_handler_outcome_unknown()
                raise ModelToolExecutionNeedsReview from None
            raise
        except BaseException:
            await fence_handler_outcome_unknown()
            raise
        if preflight_result is not None:
            result = cast(ToolCallResult, preflight_result)
            result = result.model_copy(update={"policy": result.policy or policy_payload})
            await record_audit(
                context,
                request.tool_name,
                result.invocation_id,
                result.status,
            )
            return await finish_event(result)

    try:
        raw_result = invoke_handler(tool.handler, request, context)
        if inspect.isawaitable(raw_result):
            raw_result = await raw_result
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
            policy=policy_payload,
        )
    except Exception as exc:  # noqa: BLE001 - 模型工具异常必须进入耐久未知围栏
        if claim_service is not None and execution_permit is not None:
            await fence_handler_outcome_unknown()
            raise ModelToolExecutionNeedsReview from None
        result = error_result(
            request,
            context,
            invocation_id,
            result_source_ref,
            ToolErrorCode.EXECUTION_FAILED,
            str(redact_secrets(str(exc))),
            policy=policy_payload,
        )
    except BaseException:
        # 取消发生在permit消费后时，先持久化未知围栏，再保留原取消语义给上层。
        await fence_handler_outcome_unknown()
        raise
    else:
        try:
            if isinstance(raw_result, ToolCallResult):
                raw_result = redact_tool_result(raw_result)
                result = raw_result.model_copy(
                    update={"policy": raw_result.policy or policy_payload}
                )
            else:
                payload, artifact_ref, truncation = guarded_tool_payload(
                    tool_name=request.tool_name,
                    invocation_id=invocation_id,
                    payload=raw_result,
                    artifact_store=artifact_store,
                    inline_bytes=inline_result_bytes,
                )
                result = ToolCallResult(
                    tool_name=request.tool_name,
                    status="completed",
                    invocation_id=invocation_id,
                    result=payload,
                    source_ref=result_source_ref,
                    artifact_ref=artifact_ref,
                    truncation=truncation,
                    policy=policy_payload,
                    request_id=context.request_id or request.request_id,
                    trace_id=context.trace_id or request.trace_id,
                )
        except Exception:
            if claim_service is not None and execution_permit is not None:
                await fence_handler_outcome_unknown()
                raise ModelToolExecutionNeedsReview from None
            raise
        except BaseException:
            await fence_handler_outcome_unknown()
            raise

    await record_audit(
        context,
        request.tool_name,
        result.invocation_id,
        result.status,
    )
    return await finish_event(result)


__all__ = ["call_unapproved_tool"]
