"""ToolRegistry：统一工具发现、策略检查、错误码和输出元数据。"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Literal, NoReturn, Protocol, cast

from jsonschema import Draft202012Validator

from agent_harness.artifacts import FileArtifactStore
from agent_harness.audit import AuditService
from agent_harness.models.structured import compile_output_schema_definition
from agent_harness.models.tool_catalog import (
    ToolCatalog,
    ToolCatalogConflictError,
    ToolCatalogSourceDescriptor,
    provider_tool_catalog_bytes,
)
from agent_harness.models.tool_intent import ToolIntent
from agent_harness.policy import PolicyEngine
from agent_harness.runtime.executor import ApprovalGrant
from agent_harness.storage import SQLAlchemyStorage
from agent_harness.tools._registry_call import call_unapproved_tool
from agent_harness.tools.approved_execution import ApprovedToolExecutor
from agent_harness.tools.durable_execution import model_tool_execution_lock
from agent_harness.tools.types import (
    BuiltinTool,
    ResolvedToolIntent,
    ToolCallRequest,
    ToolCallResult,
    ToolDescriptor,
    ToolIntentResolutionError,
    ToolRuntimeContext,
)

if TYPE_CHECKING:
    from agent_harness.events.model_tool_loop import (
        ModelToolLoopEventProducer,
    )


_VALIDATION_LOGGER = logging.getLogger("agent_harness.tools.registry.validation")
_LOWER_HEX = frozenset("0123456789abcdef")
_IntentResolutionCode = Literal[
    "tool.not_found",
    "tool.allowlist_denied",
    "tool.schema_validation_failed",
    "model.tool_catalog_conflict",
]


def _safe_intent_correlation(intent: object) -> dict[str, str | int]:
    """只从真实DTO内部字典提取已校验关联身份，避免错误路径触发自定义访问器。"""

    if type(intent) is not ToolIntent:
        return {}
    try:
        state = object.__getattribute__(intent, "__dict__")
    except (AttributeError, TypeError):
        return {}
    if type(state) is not dict:
        return {}
    typed_state = cast(dict[str, object], state)
    correlation: dict[str, str | int] = {}
    for field in ("loop_id", "tool_call_id", "catalog_digest"):
        value = typed_state.get(field)
        if (
            type(value) is str
            and len(value) == 64
            and all(character in _LOWER_HEX for character in value)
        ):
            correlation[field] = value
    turn_ordinal = typed_state.get("turn_ordinal")
    if type(turn_ordinal) is int and turn_ordinal >= 1:
        correlation["turn_ordinal"] = turn_ordinal
    return correlation


def record_tool_intent_validation_failure(
    code: _IntentResolutionCode,
    *,
    intent: object,
) -> None:
    """为Registry前后共用的意图拒绝路径写最小脱敏校验证据。

    这里故意不调用异步 ``AuditService``：catalog 恢复校验和 resolve seam 都必须
    保持同步只读，且在 Policy、claim、preflight、handler 之前完成。日志只含固定
    动作/错误码与严格校验后的哈希关联身份，不记录 tool name、arguments、schema
    或原始异常。
    """

    summary: dict[str, str | int] = {
        "action": "tool.intent.validation",
        "code": code,
        **_safe_intent_correlation(intent),
    }
    _VALIDATION_LOGGER.warning(
        "%s",
        json.dumps(summary, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
    )


def _reject_intent(code: _IntentResolutionCode, *, intent: object) -> NoReturn:
    """写最小脱敏校验证据后以稳定错误码关闭失败。"""

    record_tool_intent_validation_failure(code, intent=intent)
    raise ToolIntentResolutionError(code)


class _JsonSchemaValidator(Protocol):
    """封闭 jsonschema 动态类型，只暴露 Registry 所需的纯校验能力。"""

    def is_valid(self, instance: object) -> bool: ...


class ToolRegistry:
    """工具执行的进程内注册表，不让调用方直接碰具体 adapter。"""

    def __init__(
        self,
        *,
        tools: list[BuiltinTool],
        policy: PolicyEngine,
        audit: AuditService | None,
        artifact_store: FileArtifactStore,
        inline_result_bytes: int = 8192,
        agent_tool_allowlist: list[str] | None = None,
        enforce_agent_tool_allowlist: bool = False,
        storage: SQLAlchemyStorage | None = None,
    ) -> None:
        """注册工具及其安全协作者，保留可选持久化 seam 给审批后执行路径。

        allowlist 只有在显式启用时才限制工具发现和调用，避免历史 local profile 因
        空配置失去工具；真正的授权仍由 policy engine 在每次调用时判定。
        """

        self._tools = {tool.name: tool for tool in tools}
        self._policy = policy
        self._audit = audit
        self._artifact_store = artifact_store
        self._inline_result_bytes = inline_result_bytes
        self._agent_tool_allowlist = set(agent_tool_allowlist or [])
        self._enforce_agent_tool_allowlist = enforce_agent_tool_allowlist
        self._storage = storage

    def list_tools(self) -> list[ToolDescriptor]:
        """返回按名称排序的工具描述，供 CLI 和 runtime allowlist 使用。"""

        return [
            ToolDescriptor(
                name=tool.name,
                action=tool.action,
                resource=tool.resource,
                input_schema=tool.input_schema,
            )
            for tool in (self._tools[name] for name in sorted(self._tools))
            if self._is_agent_tool_allowed(tool.name)
        ]

    def catalog_descriptors(self) -> tuple[ToolCatalogSourceDescriptor, ...]:
        """返回模型目录可消费的严格只读描述，不触发任何工具协作者。

        Legacy人工工具可没有schema identity；若Agent把这类工具投影到模型
        catalog，后续交集构造会因描述缺失而关闭失败，不能临时猜ref/version。
        """

        descriptors: list[ToolCatalogSourceDescriptor] = []
        for ordinal, name in enumerate(sorted(self._tools)):
            tool = self._tools[name]
            if not self._is_agent_tool_allowed(name):
                continue
            if tool.input_schema_ref is None or tool.input_schema_version is None:
                continue
            try:
                schema = compile_output_schema_definition(
                    tool.input_schema,
                    schema_ref=tool.input_schema_ref,
                    version=tool.input_schema_version,
                )
            except ValueError:
                raise ToolCatalogConflictError from None
            descriptors.append(
                ToolCatalogSourceDescriptor(
                    name=tool.name,
                    action=tool.action,
                    resource=tool.resource,
                    input_schema=schema,
                    registry_ordinal=ordinal,
                )
            )
        return tuple(descriptors)

    def resolve_intent(
        self,
        intent: ToolIntent,
        *,
        catalog: ToolCatalog,
    ) -> ResolvedToolIntent:
        """只读重验意图、catalog与当前Registry事实，不执行任何工具步骤。"""

        try:
            if type(intent) is not ToolIntent or type(catalog) is not ToolCatalog:
                raise ValueError
            snapshot = ToolIntent.model_validate(
                ToolIntent.model_dump(intent, mode="python")
            ).model_copy(deep=True)
            # 同时深拷贝重验schema body、catalog digest与连续ordinal。
            provider_tool_catalog_bytes(catalog)
        except (AttributeError, TypeError, ValueError, ToolCatalogConflictError):
            _reject_intent("model.tool_catalog_conflict", intent=intent)
        tool = self._tools.get(snapshot.tool_name)
        if tool is None:
            _reject_intent("tool.not_found", intent=snapshot)
        if not self._is_agent_tool_allowed(snapshot.tool_name):
            _reject_intent("tool.allowlist_denied", intent=snapshot)
        if snapshot.catalog_digest != catalog.catalog_digest:
            _reject_intent("model.tool_catalog_conflict", intent=snapshot)
        entries = [item for item in catalog.tools if item.name == snapshot.tool_name]
        if len(entries) != 1:
            _reject_intent("model.tool_catalog_conflict", intent=snapshot)
        entry = entries[0]
        if tool.input_schema_ref is None or tool.input_schema_version is None:
            _reject_intent("model.tool_catalog_conflict", intent=snapshot)
        try:
            current_schema = compile_output_schema_definition(
                tool.input_schema,
                schema_ref=tool.input_schema_ref,
                version=tool.input_schema_version,
            )
        except ValueError:
            _reject_intent("model.tool_catalog_conflict", intent=snapshot)
        if (
            snapshot.tool_schema_ref != entry.input_schema_ref
            or snapshot.tool_schema_version != entry.input_schema_version
            or snapshot.tool_schema_digest != entry.input_schema_digest
            or current_schema != entry.input_schema
            or tool.action != entry.action
            or tool.resource != entry.resource
        ):
            _reject_intent("model.tool_catalog_conflict", intent=snapshot)
        validator = cast(
            _JsonSchemaValidator,
            Draft202012Validator(current_schema.schema_definition),
        )
        if not validator.is_valid(snapshot.arguments):
            _reject_intent("tool.schema_validation_failed", intent=snapshot)
        return ResolvedToolIntent(
            loop_id=snapshot.loop_id,
            turn_ordinal=snapshot.turn_ordinal,
            tool_call_id=snapshot.tool_call_id,
            tool_name=snapshot.tool_name,
            arguments=snapshot.arguments,
            arguments_digest=snapshot.arguments_digest,
            tool_schema_ref=snapshot.tool_schema_ref,
            tool_schema_version=snapshot.tool_schema_version,
            tool_schema_digest=snapshot.tool_schema_digest,
            model_usage_call_id=snapshot.model_usage_call_id,
            catalog_digest=snapshot.catalog_digest,
            action=entry.action,
            resource=entry.resource,
        )

    def _execution_request(
        self,
        request: ToolCallRequest | ResolvedToolIntent,
        *,
        context: ToolRuntimeContext,
        intent: ToolIntent | None,
        catalog: ToolCatalog | None,
    ) -> ToolCallRequest:
        """在任何execution/policy/audit步骤前重验resolve结果与当前Registry。"""

        if isinstance(request, ResolvedToolIntent):
            if intent is None or catalog is None:
                _reject_intent("model.tool_catalog_conflict", intent=intent)
            try:
                if type(request) is not ResolvedToolIntent:
                    raise ValueError
                snapshot = ResolvedToolIntent.model_validate(
                    ResolvedToolIntent.model_dump(request, mode="python")
                ).model_copy(deep=True)
            except (AttributeError, TypeError, ValueError):
                _reject_intent("model.tool_catalog_conflict", intent=intent)
            if self.resolve_intent(intent, catalog=catalog) != snapshot:
                _reject_intent("model.tool_catalog_conflict", intent=intent)
            return ToolCallRequest(
                tool_name=snapshot.tool_name,
                arguments=snapshot.arguments,
                agent_id=context.agent_id,
                run_id=context.run_id,
                request_id=context.request_id,
                trace_id=context.trace_id,
            )
        if intent is not None or catalog is not None:
            _reject_intent("model.tool_catalog_conflict", intent=intent)
        return request

    async def call(
        self,
        request: ToolCallRequest | ResolvedToolIntent,
        *,
        context: ToolRuntimeContext,
        intent: ToolIntent | None = None,
        catalog: ToolCatalog | None = None,
        events: ModelToolLoopEventProducer | None = None,
    ) -> ToolCallResult:
        """经 Registry 的唯一未批准执行协作者完成策略、claim 与结果守卫。"""

        async def invoke() -> ToolCallResult:
            """在可选模型工具execution锁内运行既有Registry完整边界。"""

            return await call_unapproved_tool(
                request,
                context=context,
                intent=intent,
                catalog=catalog,
                events=events,
                tools=self._tools,
                policy_engine=self._policy,
                storage=self._storage,
                artifact_store=self._artifact_store,
                inline_result_bytes=self._inline_result_bytes,
                is_agent_tool_allowed=self._is_agent_tool_allowed,
                execution_request=self._execution_request,
                record_audit=self._record_audit,
            )

        resolved = request if type(request) is ResolvedToolIntent else None
        if resolved is None:
            return await invoke()
        # 先执行纯intent/catalog/schema/allowlist重验，再取得跨进程execution锁；
        # 锁覆盖Policy、claim、permit、handler与result/event封存，使合法并发输家
        # 等待唯一结果。崩溃后锁自动释放，durable executing仍进入needs-review。
        self._execution_request(
            request,
            context=context,
            intent=intent,
            catalog=catalog,
        )
        if self._storage is None:
            raise RuntimeError("model tool execution requires durable storage")
        async with model_tool_execution_lock(
            self._storage,
            tenant_id=context.actor.tenant_id,
            tool_call_id=resolved.tool_call_id,
            approval_id=None,
        ):
            return await invoke()

    async def call_approved(
        self,
        request: ToolCallRequest | ResolvedToolIntent,
        *,
        context: ToolRuntimeContext,
        grant: ApprovalGrant,
        intent: ToolIntent | None = None,
        catalog: ToolCatalog | None = None,
        events: ModelToolLoopEventProducer | None = None,
    ) -> ToolCallResult:
        """在持久化 at-most-once claim 后执行一次 approved action。"""

        resolved_request = request if type(request) is ResolvedToolIntent else None
        if events is not None and (intent is None or resolved_request is None):
            raise ValueError("model tool events require a resolved intent")
        request = self._execution_request(
            request,
            context=context,
            intent=intent,
            catalog=catalog,
        )
        executor = ApprovedToolExecutor(
            tools=self._tools,
            storage=self._storage,
            artifact_store=self._artifact_store,
            audit=self._audit,
            inline_result_bytes=self._inline_result_bytes,
            agent_tool_allowlist=self._agent_tool_allowlist,
            enforce_agent_tool_allowlist=self._enforce_agent_tool_allowlist,
        )
        return await executor.execute(
            request,
            context=context,
            grant=grant,
            events=events,
            intent=intent,
            resolved=resolved_request,
        )

    async def _record_audit(
        self,
        context: ToolRuntimeContext,
        tool_name: str,
        invocation_id: str,
        status: str,
    ) -> None:
        """在审计服务存在时记录最小化调用元数据；审计关闭不阻塞工具主流程。"""

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
        """按显式开关应用 agent allowlist，关闭时维持兼容的全量工具可见性。"""

        if not self._enforce_agent_tool_allowlist:
            return True
        return tool_name in self._agent_tool_allowlist
