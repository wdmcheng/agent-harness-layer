"""模型工具循环的Execution职责。"""
# pyright: reportPrivateUsage=false, reportUnusedClass=false

from __future__ import annotations

from typing import cast

from pydantic import TypeAdapter, ValidationError

from agent_harness.events.model_tool_loop import ModelToolLoopEventPublishPending
from agent_harness.models.providers import ModelRequest, ModelResponse
from agent_harness.models.route_chain_identity import model_route_operation_identity_digest
from agent_harness.models.tool_catalog import (
    ToolCatalog,
)
from agent_harness.models.tool_intent import (
    FinalStructuredTurnResult,
    FinalTextTurnResult,
    ModelTurnResult,
    ToolIntent,
    ToolIntentTurnResult,
)
from agent_harness.models.usage import stable_usage_call_id
from agent_harness.runtime._model_tool_loop_contracts import (
    ModelToolLoopApprovalRequired,
    ModelToolLoopError,
    ModelToolLoopLimitState,
    _approval_snapshot,
    _guard_tool_result,
    _ModelToolLoopFinal,
    _next_turn_request,
    _tool_result_fragment,
)
from agent_harness.runtime._model_tool_loop_mixin_base import _ModelToolLoopMixinBase
from agent_harness.tools.types import ToolErrorCode


class _ModelToolLoopExecutionMixin(_ModelToolLoopMixinBase):
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
        """从冻结ordinal线性推进；首次与审批恢复共享唯一实现。"""

        adapter: TypeAdapter[ModelTurnResult] = TypeAdapter(ModelTurnResult)
        for turn_ordinal in range(start_turn_ordinal, limit_state.max_turns + 1):
            self._check_deadline(limit_state)
            turn_input_state = (
                settled_turn_input_state
                if turn_ordinal == start_turn_ordinal and settled_turn_input_state is not None
                else limit_state
            )
            self._check_model_budget_remaining(turn_input_state)
            turn_operation_key = f"{operation_key}:model-turn:{turn_ordinal}"
            usage_call_id = stable_usage_call_id(
                context=self._context,
                operation_key=turn_operation_key,
            )
            remaining_cost = (
                None
                if turn_input_state.max_total_cost_usd is None
                else turn_input_state.max_total_cost_usd - (turn_input_state.total_cost_usd or 0.0)
            )
            try:
                raw_result = await self._model_turns.complete_tool_loop_turn(
                    current_request,
                    context=self._context,
                    usage_call_id=usage_call_id,
                    loop_id=loop_id,
                    turn_ordinal=turn_ordinal,
                    operation_identity_digest=model_route_operation_identity_digest(
                        tenant_id=self._context.tenant_id,
                        run_id=self._context.run_id,
                        agent_id=self._context.agent_id,
                        request_id=self._context.request_id,
                        trace_id=self._context.trace_id,
                        operation_key=turn_operation_key,
                    ),
                    tool_catalog=catalog,
                    actor=self._identity,
                    loop_token_bound=(
                        turn_input_state.max_total_tokens - turn_input_state.total_tokens_used
                    ),
                    loop_cost_bound=remaining_cost,
                )
            except Exception as exc:
                code = getattr(exc, "code", None)
                if code in {
                    "model.tool_loop_limit_exceeded",
                    "model.tool_loop_needs_review",
                }:
                    raise ModelToolLoopError(cast(str, code)) from None
                raise
            try:
                result = adapter.validate_python(raw_result)
            except (TypeError, ValidationError, ValueError):
                raise ModelToolLoopError("model.tool_intent_invalid") from None
            if turn_input_state is limit_state:
                limit_state = await self._account_turn_usage(
                    limit_state,
                    usage_call_id=usage_call_id,
                    loop_id=loop_id,
                    turn_ordinal=turn_ordinal,
                )
            try:
                self._check_deadline(limit_state)
            except ModelToolLoopError:
                await self._expire_durable_model_turn(
                    loop_id=loop_id,
                    turn_ordinal=turn_ordinal,
                    usage_call_id=usage_call_id,
                    limit_state=limit_state,
                )
                raise
            await self._settle_durable_model_turn(
                loop_id=loop_id,
                turn_ordinal=turn_ordinal,
                usage_call_id=usage_call_id,
                limit_state=limit_state,
            )
            if isinstance(result, FinalTextTurnResult):
                if not self._response_matches_request(result.response, current_request):
                    raise ModelToolLoopError("model.tool_intent_invalid")
                self._observe("final_text")
                return _ModelToolLoopFinal(
                    response=result.response,
                    turn_ordinal=turn_ordinal,
                    usage_call_id=usage_call_id,
                    limit_state=limit_state,
                )
            if isinstance(result, FinalStructuredTurnResult):
                raise ModelToolLoopError("model.tool_intent_invalid")
            if type(result) is not ToolIntentTurnResult:
                raise ModelToolLoopError("model.tool_intent_invalid")
            intent = result.intent
            if (
                intent.loop_id != loop_id
                or intent.turn_ordinal != turn_ordinal
                or intent.model_usage_call_id != usage_call_id
                or intent.catalog_digest != catalog.catalog_digest
            ):
                raise ModelToolLoopError("model.tool_intent_invalid")
            self._check_tool_can_continue(
                limit_state,
                turn_ordinal=turn_ordinal,
            )
            entry = self._catalog_entry(intent, catalog=catalog)
            self._observe("tool_intent.validated")
            registry = self._tool_registry_resolver(self._context.agent_id, intent.tool_name)
            resolved = registry.resolve_intent(intent, catalog=catalog)
            if not self._resolved_matches_intent(resolved, intent=intent, entry=entry):
                raise ModelToolLoopError("model.tool_intent_invalid")
            try:
                tool_result = await registry.call(
                    resolved,
                    context=self._tool_context(),
                    intent=intent,
                    catalog=catalog,
                    events=self._loop_events,
                )
            except Exception as exc:
                if getattr(exc, "code", None) == "tool.execution_needs_review":
                    raise ModelToolLoopError("model.tool_loop_needs_review") from None
                raise
            if (
                tool_result.error is not None
                and tool_result.error.code == ToolErrorCode.APPROVAL_REQUIRED
            ):
                if self._approval_store is None:
                    raise ModelToolLoopError(ToolErrorCode.APPROVAL_REQUIRED.value)
                snapshot = _approval_snapshot(
                    operation_key=operation_key,
                    initial_request=initial_request,
                    current_request=current_request,
                    context=self._context,
                    identity_id=self._identity.user_id,
                    session_id=self._identity.session_id,
                    intent=intent,
                    catalog=catalog,
                    action=entry.action,
                    resource=entry.resource,
                    limits=limit_state,
                )
                approval = self._approval_store.create(
                    snapshot=snapshot,
                    reason=tool_result.error.message,
                )
                raise ModelToolLoopApprovalRequired(approval, snapshot=snapshot)
            current_request, context_ref = await self._request_after_tool_result(
                current_request,
                tool_result=tool_result,
                intent=intent,
                expected_tool_name=intent.tool_name,
                limit_state=limit_state,
            )
            await self._commit_durable_turn(
                loop_id=loop_id,
                turn_ordinal=turn_ordinal,
                limit_state=limit_state,
                next_request=current_request,
                model_usage_call_id=usage_call_id,
                tool_call_id=intent.tool_call_id,
                approval_id=None,
                checkpoint_ref=None,
                context_ref=context_ref,
            )
        raise ModelToolLoopError("model.tool_loop_limit_exceeded")

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
        """把全部循环出口收敛到唯一durable waiting/terminal提交点。"""

        try:
            final = await self._continue(
                initial_request=initial_request,
                current_request=current_request,
                operation_key=operation_key,
                catalog=catalog,
                loop_id=loop_id,
                start_turn_ordinal=start_turn_ordinal,
                limit_state=limit_state,
                settled_turn_input_state=settled_turn_input_state,
            )
        except ModelToolLoopApprovalRequired as waiting:
            await self._wait_durable_loop(
                loop_id,
                approval=waiting.approval,
                snapshot=waiting.snapshot,
            )
            raise
        except ModelToolLoopEventPublishPending:
            # exact event intent已耐久，不能把可补投窗口永久写成needs-review。
            raise
        except ModelToolLoopError as failure:
            status = (
                "needs_review"
                if failure.args and failure.args[0] == "model.tool_loop_needs_review"
                else "failed"
            )
            await self._fail_durable_loop(loop_id, status=status, code=str(failure))
            raise
        except BaseException:
            await self._fail_durable_loop(
                loop_id,
                status="needs_review",
                code="model.tool_loop_needs_review",
            )
            raise
        await self._complete_durable_loop(
            loop_id,
            operation_key=operation_key,
            final=final,
        )
        return final.response

    async def _request_after_tool_result(
        self,
        current_request: ModelRequest,
        *,
        tool_result: object,
        intent: ToolIntent,
        expected_tool_name: str,
        limit_state: ModelToolLoopLimitState,
    ) -> tuple[ModelRequest, str]:
        """统一验证结果、构造untrusted fragment并经ContextAssembler回注。"""

        tool_result = _guard_tool_result(
            tool_result,
            expected_tool_name=expected_tool_name,
        )
        self._check_deadline(limit_state)
        if tool_result.status != "completed":
            code = tool_result.error.code.value if tool_result.error is not None else "tool.failed"
            raise ModelToolLoopError(code)
        fragment = _tool_result_fragment(tool_result)
        # 输出上限约束实际送入 ContextAssembler 的 canonical UTF-8 内容；即使
        # handler 已把原正文转成 artifact，引用本身也不能绕过调用方缩小后的边界。
        if len(fragment.content.encode("utf-8")) > limit_state.max_tool_output_bytes:
            raise ModelToolLoopError("model.tool_loop_limit_exceeded")
        context_step = None
        if self._loop_events is not None:
            context_step = await self._loop_events.begin_context(
                context=self._context,
                identity_id=self._identity.user_id,
                intent=intent,
                fragment=fragment,
            )
        assembly = await self._context_assembly.assemble(
            tenant_id=self._context.tenant_id,
            run_id=self._context.run_id,
            fragments=[fragment],
            token_budget=max(1, current_request.max_output_tokens),
            loop_id=intent.loop_id,
            turn_ordinal=intent.turn_ordinal,
            tool_call_id=intent.tool_call_id,
        )
        if self._loop_events is not None and context_step is not None:
            await self._loop_events.finish_context(step=context_step, result=assembly)
        self._check_deadline(limit_state)
        return _next_turn_request(current_request, assembly=assembly), assembly.output_ref
