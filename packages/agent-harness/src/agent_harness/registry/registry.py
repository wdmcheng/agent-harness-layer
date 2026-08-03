"""多 agent registry loader 与 delegation 校验。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import Field

from agent_harness.contracts import ErrorDetail
from agent_harness.contracts.dto import HarnessDTO
from agent_harness.models.structured import OutputSchemaDefinition, compile_output_schema
from agent_harness.registry._loader import (
    RegistryLoadError as RegistryLoadError,
)
from agent_harness.registry._loader import agent_import_context as _agent_import_context
from agent_harness.registry._loader import (
    build_descriptor as _build_descriptor,
)
from agent_harness.registry._loader import (
    load_descriptor as _load_descriptor,
)
from agent_harness.registry._loader import (
    load_executor as _load_executor,
)
from agent_harness.registry._loader import load_schema as _load_schema
from agent_harness.registry._loader import resolve_executor_target as _resolve_executor_target
from agent_harness.registry._loader import resolve_schema_target as _resolve_schema_target
from agent_harness.registry.descriptor import (
    AgentDescriptor,
)
from agent_harness.runtime.executor import AgentExecutor

if TYPE_CHECKING:
    from agent_harness.registry._loader import AgentConfigRecord

# 异常仍以 registry 公开 facade 为身份，保持拆分前的诊断与序列化契约。
RegistryLoadError.__module__ = __name__


class DelegationDecision(HarnessDTO):
    """agent 互调前的 allow/deny 判断。"""

    allowed: bool
    source_agent_id: str
    target_agent_id: str
    reason: str


class DelegationSummary(HarnessDTO):
    """已声明 delegation 的 parent/child 归属摘要。"""

    parent_agent_id: str
    target_agent_id: str
    parent_run_id: str | None = None
    delegated_run_id: str | None = None
    usage_refs: list[str] = Field(default_factory=list)
    budget_summary: dict[str, Any] = Field(default_factory=dict)
    trace_refs: list[str] = Field(default_factory=list)


class AgentRegistry:
    """从模板 agent config 构造的只读 registry。"""

    def __init__(
        self,
        descriptors: Sequence[AgentDescriptor],
        *,
        executors: Mapping[str, AgentExecutor] | None = None,
        output_schemas: Mapping[str, OutputSchemaDefinition] | None = None,
    ) -> None:
        """以已验证的 descriptor 和 executor 构建只读索引。

        该构造器不执行文件加载或动态 import，便于测试和 composition root 注入；
        从磁盘构建时应使用 ``load_from_directory`` 完成全量验证后再调用这里。
        """

        self._descriptors = {descriptor.agent_id: descriptor for descriptor in descriptors}
        self._executors = dict(executors or {})
        self._output_schemas = dict(output_schemas or {})

    @classmethod
    def load_from_directory(cls, root: Path) -> AgentRegistry:
        """从受控目录加载所有 agent config，并拒绝部分可用的脏 registry。"""

        descriptor_configs: list[tuple[AgentConfigRecord, Path]] = []
        executor_refs: list[tuple[str, str, Path]] = []
        schema_refs: list[tuple[str, str, str, Path]] = []
        seen: dict[str, Path] = {}
        for config_path in sorted(root.rglob("config.yaml")):
            config, executor_ref = _load_descriptor(config_path, root=root)
            first_seen = seen.get(config.agent_id)
            if first_seen is not None:
                raise RegistryLoadError(
                    [
                        ErrorDetail(
                            code="registry.duplicate_agent_id",
                            message=f"duplicate agent_id: {config.agent_id}",
                            field_path="agent_id",
                            hint=f"检查 {first_seen} 和 {config_path}",
                        )
                    ]
                )
            seen[config.agent_id] = config_path
            descriptor_configs.append((config, config_path))
            executor_refs.append((config.agent_id, executor_ref, config_path))
            schema_refs.extend(
                [
                    (
                        config.agent_id,
                        "input_schema",
                        config.input_schema,
                        config_path,
                    ),
                    (
                        config.agent_id,
                        "output_schema",
                        config.output_schema,
                        config_path,
                    ),
                ]
            )

        # 在 import 任一目标前先解析全部 descriptor、reference 和 module path。
        # 任一 sibling 非法都必须整体拒绝，不能留下部分可运行进程。
        resolved_targets = [
            (agent_id, _resolve_executor_target(reference, config_path))
            for agent_id, reference, config_path in executor_refs
        ]
        resolved_schemas = [
            (
                agent_id,
                field_path,
                _resolve_schema_target(reference, config_path, root=root, field_path=field_path),
            )
            for agent_id, field_path, reference, config_path in schema_refs
        ]
        output_schemas: dict[str, OutputSchemaDefinition] = {}
        with _agent_import_context(root):
            for agent_id, field_path, (module_ref, attribute) in resolved_schemas:
                schema_model = _load_schema(agent_id, module_ref, attribute, field_path=field_path)
                if field_path == "output_schema":
                    config = next(
                        item for item, _path in descriptor_configs if item.agent_id == agent_id
                    )
                    try:
                        output_schemas[agent_id] = compile_output_schema(
                            schema_model,
                            schema_ref=config.output_schema,
                            version=config.version,
                        )
                    except ValueError as exc:
                        raise RegistryLoadError(
                            [
                                ErrorDetail(
                                    code="registry.invalid_schema",
                                    message=str(exc),
                                    field_path="output_schema",
                                    hint=f"修正 agent schema reference：{module_ref}",
                                )
                            ]
                        ) from exc
            # Descriptor 的 route/capability/schema identity 投影仍属于全量静态预校验；
            # 必须在导入任一 executor 前全部成功，避免无效 sibling 产生模块副作用。
            descriptors = [
                _build_descriptor(
                    config,
                    config_path=config_path,
                    root=root,
                    output_schema_identity=output_schemas[config.agent_id].identity,
                )
                for config, config_path in descriptor_configs
            ]
            executors = {
                agent_id: _load_executor(agent_id, module_path, attribute)
                for agent_id, (module_path, attribute) in resolved_targets
            }
        return cls(descriptors, executors=executors, output_schemas=output_schemas)

    def list_agents(self) -> list[AgentDescriptor]:
        """按稳定 agent_id 排序返回 descriptor，避免文件系统枚举影响 API 输出。"""

        return sorted(self._descriptors.values(), key=lambda item: item.agent_id)

    def get(self, agent_id: str) -> AgentDescriptor:
        """取得声明的 agent；未知 id 统一转换为可序列化的 registry 错误。"""

        try:
            return self._descriptors[agent_id]
        except KeyError as exc:
            raise RegistryLoadError(
                [
                    ErrorDetail(
                        code="registry.agent_not_found",
                        message=f"agent not found: {agent_id}",
                        field_path="agent_id",
                    )
                ]
            ) from exc

    def resolve_executor(self, agent_id: str) -> AgentExecutor:
        """返回已验证的内部 executor，不改变 public descriptor。"""

        self.get(agent_id)
        try:
            return self._executors[agent_id]
        except KeyError as exc:
            raise RegistryLoadError(
                [
                    ErrorDetail(
                        code="registry.executor_not_found",
                        message=f"executor not found: {agent_id}",
                        field_path="executor",
                    )
                ]
            ) from exc

    def resolve_output_schema(self, agent_id: str) -> OutputSchemaDefinition:
        """返回与 public descriptor identity 逐值一致的严格 output schema。"""

        descriptor = self.get(agent_id)
        try:
            schema = self._output_schemas[agent_id]
        except KeyError as exc:
            raise RegistryLoadError(
                [
                    ErrorDetail(
                        code="registry.output_schema_not_found",
                        message=f"output schema not found: {agent_id}",
                        field_path="output_schema",
                    )
                ]
            ) from exc
        if schema.identity != descriptor.output_schema_identity:
            raise RegistryLoadError(
                [
                    ErrorDetail(
                        code="registry.output_schema_conflict",
                        message=f"output schema identity conflict: {agent_id}",
                        field_path="output_schema_identity",
                    )
                ]
            )
        return schema

    def check_delegation(self, source_agent_id: str, target_agent_id: str) -> DelegationDecision:
        """根据已加载 descriptor 判断 source 到 target 的静态 delegation 边。

        这里只验证配置图，不替代运行时身份、策略、冻结预算快照或深度限制检查；
        这些依然由 delegation application service 在实际执行前处理。
        """

        source = self.get(source_agent_id)
        if target_agent_id in source.delegation_targets:
            return DelegationDecision(
                allowed=True,
                source_agent_id=source_agent_id,
                target_agent_id=target_agent_id,
                reason="delegation edge declared",
            )
        return DelegationDecision(
            allowed=False,
            source_agent_id=source_agent_id,
            target_agent_id=target_agent_id,
            reason="delegation edge is not declared",
        )

    def delegation_summary(
        self,
        *,
        source_agent_id: str,
        target_agent_id: str,
        parent_run_id: str | None = None,
        delegated_run_id: str | None = None,
        usage_refs: Sequence[str] = (),
        budget_summary: Mapping[str, Any] | None = None,
        trace_refs: Sequence[str] = (),
    ) -> DelegationSummary:
        """生成已授权 delegation 的关系摘要，并复制调用方集合防止后续变异泄漏。

        该 helper 先重复检查静态边，确保没有调用方能绕过 registry 直接伪造
        parent/child 归属摘要；动态用量与预算值仍由上层的 durable evidence 提供。
        """

        decision = self.check_delegation(source_agent_id, target_agent_id)
        if not decision.allowed:
            raise RegistryLoadError(
                [
                    ErrorDetail(
                        code="registry.delegation_denied",
                        message=decision.reason,
                        field_path="delegation_edges",
                    )
                ]
            )
        return DelegationSummary(
            parent_agent_id=source_agent_id,
            target_agent_id=target_agent_id,
            parent_run_id=parent_run_id,
            delegated_run_id=delegated_run_id,
            usage_refs=list(usage_refs),
            budget_summary=dict(budget_summary or {}),
            trace_refs=list(trace_refs),
        )
