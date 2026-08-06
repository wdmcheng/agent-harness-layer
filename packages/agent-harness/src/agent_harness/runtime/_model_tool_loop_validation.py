"""模型工具循环的Validation职责。"""
# pyright: reportPrivateUsage=false, reportUnusedClass=false

from __future__ import annotations

from agent_harness.models.providers import ModelRequest, ModelResponse
from agent_harness.models.tool_catalog import ToolCatalog, ToolCatalogEntry
from agent_harness.models.tool_intent import (
    ToolIntent,
)
from agent_harness.runtime._model_tool_loop_contracts import (
    ModelToolLoopApprovalSnapshot,
    ModelToolLoopError,
)
from agent_harness.runtime._model_tool_loop_mixin_base import _ModelToolLoopMixinBase
from agent_harness.runtime.executor import ApprovalGrant
from agent_harness.tools.approval_identity import hash_tool_arguments
from agent_harness.tools.types import ResolvedToolIntent


class _ModelToolLoopValidationMixin(_ModelToolLoopMixinBase):
    def _approval_snapshot_matches(
        self,
        snapshot: ModelToolLoopApprovalSnapshot,
        *,
        request: ModelRequest,
        operation_key: str,
        grant: ApprovalGrant,
    ) -> bool:
        """交叉校验snapshot、当前绑定、原始请求与active grant全部身份。"""

        return (
            snapshot.operation_key == operation_key
            and snapshot.initial_request == request
            and snapshot.context == self._context
            and snapshot.identity_id == self._identity.user_id
            and snapshot.session_id == self._identity.session_id
            and snapshot.intent.loop_id == self._loop_id(operation_key)
            and grant.tenant_id == self._context.tenant_id
            and grant.identity_id == self._identity.user_id
            and grant.session_id == self._identity.session_id
            and grant.agent_id == self._context.agent_id
            and grant.run_id == self._context.run_id
            and grant.action == snapshot.action
            and grant.resource == snapshot.resource
            and grant.arguments_hash == hash_tool_arguments(snapshot.intent.arguments)
        )

    def _observe(self, step: str) -> None:
        """只发送封闭阶段名，不向观察器传递内容、身份或错误对象。"""

        if self._step_observer is not None:
            self._step_observer(step)

    def _request_matches_agent_policy(self, request: ModelRequest) -> bool:
        """请求只可沿绑定Agent的单route tool-intent授权缩权。"""

        policy = self._model_policy
        return (
            (request.provider is None or request.provider == policy.provider)
            and (request.deployment_id is None or request.deployment_id == policy.deployment_id)
            and (request.model is None or request.model in policy.allowed_models)
            and request.route_refs is None
        )

    @staticmethod
    def _catalog_entry(intent: ToolIntent, *, catalog: ToolCatalog) -> ToolCatalogEntry:
        """在Registry前先逐值绑定intent与本loop冻结catalog。"""

        entries = tuple(item for item in catalog.tools if item.name == intent.tool_name)
        if len(entries) != 1:
            raise ModelToolLoopError("model.tool_intent_invalid")
        entry = entries[0]
        if (
            intent.tool_schema_ref != entry.input_schema_ref
            or intent.tool_schema_version != entry.input_schema_version
            or intent.tool_schema_digest != entry.input_schema_digest
        ):
            raise ModelToolLoopError("model.tool_intent_invalid")
        return entry

    @staticmethod
    def _resolved_matches_intent(
        resolved: object,
        *,
        intent: ToolIntent,
        entry: object,
    ) -> bool:
        """深拷贝重验data-only resolve结果，阻止内部TOCTOU漂移。"""

        if type(resolved) is not ResolvedToolIntent or type(entry) is not ToolCatalogEntry:
            return False
        try:
            snapshot = ResolvedToolIntent.model_validate(
                ResolvedToolIntent.model_dump(resolved, mode="python")
            ).model_copy(deep=True)
        except (AttributeError, TypeError, ValueError):
            return False
        return snapshot == ResolvedToolIntent(
            loop_id=intent.loop_id,
            turn_ordinal=intent.turn_ordinal,
            tool_call_id=intent.tool_call_id,
            tool_name=intent.tool_name,
            arguments=intent.arguments,
            arguments_digest=intent.arguments_digest,
            tool_schema_ref=intent.tool_schema_ref,
            tool_schema_version=intent.tool_schema_version,
            tool_schema_digest=intent.tool_schema_digest,
            model_usage_call_id=intent.model_usage_call_id,
            catalog_digest=intent.catalog_digest,
            action=entry.action,
            resource=entry.resource,
        )

    @staticmethod
    def _response_matches_request(response: ModelResponse, request: ModelRequest) -> bool:
        """拒绝 provider/model 漂移；nullable assertion 不产生新授权。"""

        return (
            (request.provider is None or response.provider == request.provider)
            and (request.model is None or response.model == request.model)
            and response.structured_output is None
        )
