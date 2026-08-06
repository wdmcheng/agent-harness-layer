"""模型工具循环的启动与审批恢复入口。"""
# pyright: reportPrivateUsage=false, reportUnusedClass=false

from __future__ import annotations

from agent_harness.events.model_tool_loop import (
    ModelToolLoopEventPublishPending,
    ModelToolLoopEventRecoveryError,
)
from agent_harness.models.providers import ModelRequest, ModelResponse
from agent_harness.models.tool_catalog import (
    ToolCatalog,
    ToolCatalogSelection,
    provider_tool_catalog_bytes,
)
from agent_harness.runtime._model_tool_loop_contracts import (
    ModelToolLoopApprovalSnapshot,
    ModelToolLoopError,
    ModelToolLoopLimitOverrides,
    ModelToolLoopLimitState,
    _approval_snapshot_digest,
)
from agent_harness.runtime._model_tool_loop_mixin_base import _ModelToolLoopMixinBase
from agent_harness.runtime.executor import ApprovalGrant
from agent_harness.tools.types import ResolvedToolIntent


def _record_catalog_validation_failure(intent: object) -> None:
    """延迟取得Registry审计seam，避免runtime公共导出与tools初始化形成环。"""

    from agent_harness.tools.registry import record_tool_intent_validation_failure

    record_tool_intent_validation_failure(
        "model.tool_catalog_conflict",
        intent=intent,
    )


class _ModelToolLoopEntryMixin(_ModelToolLoopMixinBase):
    async def run(
        self,
        request: ModelRequest,
        *,
        operation_key: str,
        tool_selection: ToolCatalogSelection | None = None,
        limits: ModelToolLoopLimitOverrides | None = None,
    ) -> ModelResponse:
        """按单一线性 owner 推进模型、Registry、工具、Context 和下一轮。"""

        if type(request) is not ModelRequest or request.capability != "tool_intent":
            raise ModelToolLoopError("model.tool_intent_invalid")
        if self._loop_limits is None:
            # 没有 descriptor 声明就没有隐式默认；普通 fake Agent 仍可绑定公共
            # execution context，但不能借 service 名称启用 tool loop。
            raise ModelToolLoopError("model.tool_intent_invalid")
        if type(operation_key) is not str or not operation_key:
            raise ModelToolLoopError("model.tool_loop_replay_conflict")
        limit_state = self._freeze_limits(limits)
        if not self._request_matches_agent_policy(request):
            raise ModelToolLoopError("model.tool_loop_replay_conflict")
        try:
            catalog = self._tool_catalog_resolver(self._context.agent_id, tool_selection)
        except Exception as exc:
            if getattr(exc, "code", None) == "model.tool_catalog_conflict":
                raise
            raise ModelToolLoopError("model.tool_catalog_conflict") from None
        loop_id = self._loop_id(operation_key)
        durable = await self._ensure_durable_loop(
            initial_request=request,
            operation_key=operation_key,
            catalog=catalog,
            loop_id=loop_id,
            limit_state=limit_state,
        )
        if durable is not None and durable.status == "active" and self._loop_events is not None:
            try:
                await self._loop_events.recover_pending_for_run(
                    run_id=self._context.run_id,
                    loop_id=loop_id,
                )
            except ModelToolLoopEventRecoveryError:
                await self._fail_durable_loop(
                    loop_id,
                    status="needs_review",
                    code="model.tool_loop_needs_review",
                )
                raise ModelToolLoopError("model.tool_loop_needs_review") from None
            durable = await self._durable_loop(loop_id)
        if durable is not None and durable.status == "completed":
            return self._replay_completed_response(durable, initial_request=request)
        current_request = request.model_copy(deep=True)
        start_turn_ordinal = 1
        settled_turn_input_state: ModelToolLoopLimitState | None = None
        if durable is not None:
            if durable.status == "needs_review":
                raise ModelToolLoopError("model.tool_loop_needs_review")
            if durable.status != "active":
                code = (
                    durable.error_ref.removeprefix("error:")
                    if durable.error_ref is not None
                    else "model.tool_loop_replay_conflict"
                )
                raise ModelToolLoopError(code)
            try:
                restored = await self._restore_active_loop(
                    durable,
                    initial_request=request,
                    operation_key=operation_key,
                )
                current_request = restored.current_request
                start_turn_ordinal = restored.start_turn_ordinal
                limit_state = restored.limit_state
                settled_turn_input_state = restored.settled_turn_input_state
            except ModelToolLoopError as failure:
                if failure.code == "model.tool_loop_needs_review":
                    await self._fail_durable_loop(
                        loop_id,
                        status="needs_review",
                        code=failure.code,
                    )
                raise
        return await self._continue_with_terminal(
            initial_request=request.model_copy(deep=True),
            current_request=current_request,
            operation_key=operation_key,
            catalog=catalog,
            loop_id=loop_id,
            start_turn_ordinal=start_turn_ordinal,
            limit_state=limit_state,
            settled_turn_input_state=settled_turn_input_state,
        )

    async def preflight_resume(self, grant: ApprovalGrant) -> None:
        """在`run.resumed`前只读重验审批snapshot、loop、Registry与既有claim。"""

        await self._prepare_approval_resume(
            grant=grant,
            request=None,
            operation_key=None,
        )

    async def _prepare_approval_resume(
        self,
        *,
        grant: ApprovalGrant,
        request: ModelRequest | None,
        operation_key: str | None,
    ) -> tuple[
        ModelToolLoopApprovalSnapshot,
        ModelToolLoopLimitState,
        ToolCatalog,
        object,
        ResolvedToolIntent,
    ]:
        """统一恢复前置校验；该阶段不得取得工具permit或调用provider/handler。"""

        if self._approval_store is None:
            raise ModelToolLoopError("tool.approval_invalid")
        try:
            resolved_snapshot = await self._approval_store.resolve(grant=grant)
            if type(resolved_snapshot) is not ModelToolLoopApprovalSnapshot:
                raise ValueError("approval store returned an invalid snapshot")
            snapshot = resolved_snapshot.model_copy(deep=True)
            provider_tool_catalog_bytes(snapshot.catalog)
            if snapshot.snapshot_digest != _approval_snapshot_digest(
                operation_key=snapshot.operation_key,
                initial_request=snapshot.initial_request,
                current_request=snapshot.current_request,
                context=snapshot.context,
                identity_id=snapshot.identity_id,
                session_id=snapshot.session_id,
                intent=snapshot.intent,
                catalog=snapshot.catalog,
                action=snapshot.action,
                resource=snapshot.resource,
                limits=snapshot.limits,
            ):
                raise ValueError("approval snapshot changed after persistence")
        except ModelToolLoopError:
            raise
        except Exception:
            raise ModelToolLoopError("tool.approval_invalid") from None
        checked_request = snapshot.initial_request if request is None else request
        checked_operation_key = snapshot.operation_key if operation_key is None else operation_key
        if not self._approval_snapshot_matches(
            snapshot,
            request=checked_request,
            operation_key=checked_operation_key,
            grant=grant,
        ):
            raise ModelToolLoopError("tool.approval_invalid")
        limit_state = snapshot.limits.model_copy(deep=True)
        try:
            self._check_deadline(limit_state)
            self._check_tool_can_continue(
                limit_state,
                turn_ordinal=snapshot.intent.turn_ordinal,
            )
        except ModelToolLoopError as failure:
            await self._fail_durable_loop(
                snapshot.intent.loop_id,
                status="failed",
                code=failure.code,
            )
            raise
        try:
            current_catalog = self._tool_catalog_resolver(
                self._context.agent_id,
                ToolCatalogSelection(
                    tool_names=tuple(item.name for item in snapshot.catalog.tools),
                ),
            )
        except Exception:
            _record_catalog_validation_failure(snapshot.intent)
            await self._fail_durable_loop(
                snapshot.intent.loop_id,
                status="failed",
                code="model.tool_catalog_conflict",
            )
            raise ModelToolLoopError("model.tool_catalog_conflict") from None
        if current_catalog != snapshot.catalog:
            _record_catalog_validation_failure(snapshot.intent)
            await self._fail_durable_loop(
                snapshot.intent.loop_id,
                status="failed",
                code="model.tool_catalog_conflict",
            )
            raise ModelToolLoopError("model.tool_catalog_conflict")
        durable = await self._ensure_durable_loop(
            initial_request=snapshot.initial_request,
            operation_key=snapshot.operation_key,
            catalog=current_catalog,
            loop_id=snapshot.intent.loop_id,
            limit_state=limit_state,
        )
        if durable is not None and durable.status not in {"waiting_approval", "active"}:
            code = (
                "model.tool_loop_needs_review"
                if durable.status == "needs_review"
                else "model.tool_loop_replay_conflict"
            )
            raise ModelToolLoopError(code)
        try:
            registry = self._tool_registry_resolver(
                self._context.agent_id,
                snapshot.intent.tool_name,
            )
            resolved = registry.resolve_intent(snapshot.intent, catalog=current_catalog)
            entry = self._catalog_entry(snapshot.intent, catalog=current_catalog)
            if not self._resolved_matches_intent(
                resolved,
                intent=snapshot.intent,
                entry=entry,
            ):
                raise ModelToolLoopError("model.tool_intent_invalid")
            await self._validate_existing_approval_claim(
                grant=grant,
                resolved=resolved,
            )
        except ModelToolLoopError as failure:
            status = "needs_review" if failure.code == "model.tool_loop_needs_review" else "failed"
            await self._fail_durable_loop(
                snapshot.intent.loop_id,
                status=status,
                code=failure.code,
            )
            raise
        except Exception as exc:
            code = getattr(exc, "code", "model.tool_intent_invalid")
            await self._fail_durable_loop(
                snapshot.intent.loop_id,
                status="failed",
                code=str(code),
            )
            raise ModelToolLoopError(str(code)) from None
        return snapshot, limit_state, current_catalog, registry, resolved

    async def _validate_existing_approval_claim(
        self,
        *,
        grant: ApprovalGrant,
        resolved: ResolvedToolIntent,
    ) -> None:
        """在恢复事件前逐值重验nullable tool claim，不取得lease或permit。"""

        if self._storage is None:
            return
        async with self._storage.uow() as uow:
            existing = await uow.tool_invocations.get_by_approval_id(grant.approval_id)
        if existing is None:
            return
        from agent_harness.tools.durable_execution import build_model_tool_invocation_claim

        expected = build_model_tool_invocation_claim(
            resolved=resolved,
            context=self._tool_context(),
            args_ref=existing.args_ref,
            approval_id=grant.approval_id,
            metadata=existing.metadata,
        )
        exact_fields = (
            "tenant_id",
            "agent_id",
            "run_id",
            "tool_name",
            "approval_id",
            "arguments_hash",
            "trace_id",
            "request_id",
            "loop_id",
            "turn_ordinal",
            "tool_call_id",
            "binding",
        )
        if any(getattr(existing, name) != getattr(expected, name) for name in exact_fields):
            raise ModelToolLoopError("model.tool_loop_replay_conflict")
        if existing.execution_state in {"executing", "needs_review"}:
            raise ModelToolLoopError("model.tool_loop_needs_review")
        if existing.execution_state == "claimed":
            if existing.handler_started_at is not None:
                raise ModelToolLoopError("model.tool_loop_needs_review")
            if existing.execution_lease_expires_at is None:
                raise ModelToolLoopError("model.tool_loop_needs_review")
            now = self._trusted_clock()
            if now.tzinfo is None or now.utcoffset() is None:
                raise ModelToolLoopError("model.tool_loop_limit_invalid")
            if (
                now.astimezone(existing.execution_lease_expires_at.tzinfo)
                < existing.execution_lease_expires_at
            ):
                raise ModelToolLoopError("model.tool_loop_needs_review")
            return
        if existing.execution_state not in {"completed", "failed"} or existing.result_ref is None:
            raise ModelToolLoopError("model.tool_loop_needs_review")

    async def resume(
        self,
        request: ModelRequest,
        *,
        operation_key: str,
        grant: ApprovalGrant,
    ) -> ModelResponse:
        """从既有ApprovalService active lease恢复原工具意图并继续下一模型轮。"""

        if (
            type(request) is not ModelRequest
            or request.capability != "tool_intent"
            or type(operation_key) is not str
            or not operation_key
            or not self._request_matches_agent_policy(request)
        ):
            raise ModelToolLoopError("model.tool_loop_replay_conflict")
        (
            snapshot,
            limit_state,
            current_catalog,
            registry,
            resolved,
        ) = await self._prepare_approval_resume(
            grant=grant,
            request=request,
            operation_key=operation_key,
        )
        await self._resume_durable_loop(
            snapshot.intent.loop_id,
            approval_id=grant.approval_id,
        )
        try:
            call_approved = getattr(registry, "call_approved", None)
            if call_approved is None:
                raise ModelToolLoopError("tool.approval_invalid")
            try:
                tool_result = await call_approved(
                    resolved,
                    context=self._tool_context(),
                    grant=grant,
                    intent=snapshot.intent,
                    catalog=current_catalog,
                    events=self._loop_events,
                )
            except Exception as exc:
                if getattr(exc, "code", None) == "tool.execution_needs_review":
                    raise ModelToolLoopError("model.tool_loop_needs_review") from None
                raise
            current_request, context_ref = await self._request_after_tool_result(
                snapshot.current_request,
                tool_result=tool_result,
                intent=snapshot.intent,
                expected_tool_name=snapshot.intent.tool_name,
                limit_state=limit_state,
            )
            await self._commit_durable_turn(
                loop_id=snapshot.intent.loop_id,
                turn_ordinal=snapshot.intent.turn_ordinal,
                limit_state=limit_state,
                next_request=current_request,
                model_usage_call_id=snapshot.intent.model_usage_call_id,
                tool_call_id=snapshot.intent.tool_call_id,
                approval_id=grant.approval_id,
                checkpoint_ref=f"model-tool-loop-snapshot:{snapshot.snapshot_digest}",
                context_ref=context_ref,
            )
        except ModelToolLoopEventPublishPending:
            # 与普通入口一致：exact outbox等待下一次runtime补投，不消费第二次grant。
            raise
        except ModelToolLoopError as failure:
            status = "needs_review" if failure.code == "model.tool_loop_needs_review" else "failed"
            await self._fail_durable_loop(
                snapshot.intent.loop_id,
                status=status,
                code=failure.code,
            )
            raise
        except BaseException:
            await self._fail_durable_loop(
                snapshot.intent.loop_id,
                status="needs_review",
                code="model.tool_loop_needs_review",
            )
            raise
        return await self._continue_with_terminal(
            initial_request=snapshot.initial_request,
            current_request=current_request,
            operation_key=operation_key,
            catalog=current_catalog,
            loop_id=snapshot.intent.loop_id,
            start_turn_ordinal=snapshot.intent.turn_ordinal + 1,
            limit_state=limit_state,
        )
