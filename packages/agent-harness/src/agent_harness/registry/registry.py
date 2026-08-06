"""多 agent registry loader 与 delegation 校验。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import Field

from agent_harness.config.schemas import ModelSettings
from agent_harness.contracts import ErrorDetail
from agent_harness.contracts.dto import HarnessDTO
from agent_harness.models.structured import OutputSchemaDefinition, compile_output_schema
from agent_harness.models.tool_catalog import (
    ToolCatalog,
    ToolCatalogConflictError,
    ToolCatalogSelection,
    ToolCatalogSourceDescriptor,
    build_tool_catalog,
)
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


def _validate_tool_loop_capabilities(
    descriptor_configs: Sequence[tuple[AgentConfigRecord, Path]],
    *,
    model_settings: ModelSettings | None,
) -> None:
    """以 typed deployment capability 校验 ``model_tool_loop`` required-iff。

    旧调用方未提供 settings 时仍可加载没有循环对象的 legacy/fake Agent；但一旦
    YAML 声明循环对象，就必须由受信配置证明对应 route 支持 ``tool_intent``。
    """

    for config, config_path in descriptor_configs:
        loop_declared = config.model_tool_loop is not None
        if model_settings is None:
            if loop_declared:
                raise _tool_loop_config_error(
                    config_path,
                    "model_tool_loop requires typed model settings capability validation",
                )
            continue

        deployment_ids = (
            [route.deployment_id for route in config.model.fallback_routes]
            if config.model.fallback_routes
            else [config.model.deployment_id]
        )
        try:
            supports_tool_intent = any(
                "tool_intent" in model_settings.deployments[deployment_id].capabilities
                for deployment_id in deployment_ids
            )
        except KeyError as exc:
            raise _tool_loop_config_error(
                config_path,
                f"unknown model deployment: {exc.args[0]}",
                field_path="model.deployment_id",
            ) from exc

        if supports_tool_intent and config.model.fallback_routes:
            raise _tool_loop_config_error(
                config_path,
                "tool_intent agents must not declare fallback_routes",
                field_path="model.fallback_routes",
            )

        if supports_tool_intent != loop_declared:
            requirement = "required" if supports_tool_intent else "forbidden"
            raise _tool_loop_config_error(
                config_path,
                f"model_tool_loop is {requirement} for the configured model capability",
            )


def _build_agent_tool_catalogs(
    descriptor_configs: Sequence[tuple[AgentConfigRecord, Path]],
    *,
    tool_catalog_descriptors: tuple[ToolCatalogSourceDescriptor, ...] | None,
) -> dict[str, ToolCatalog]:
    """在 executor import 前冻结每个 tool-loop Agent 的授权目录。

    只有声明 ``model_tool_loop`` 的 Agent 使用受控模型工具目录；legacy
    Agent 的普通工具 allowlist 继续由既有运行时授权处理。非空模型工具权限必须
    由 composition 提供同一 ToolRegistry 的 data-only descriptors，不能把目录
    校验推迟到首个模型请求。
    """

    catalogs: dict[str, ToolCatalog] = {}
    for config, config_path in descriptor_configs:
        if config.model_tool_loop is None:
            continue
        if tool_catalog_descriptors is None and config.tool_allowlist:
            raise _tool_loop_config_error(
                config_path,
                "model tool catalog descriptors are required for non-empty tool_allowlist",
                field_path="tool_allowlist",
            )
        try:
            catalogs[config.agent_id] = build_tool_catalog(
                allowed_tools=tuple(config.tool_allowlist),
                registry_descriptors=tool_catalog_descriptors or (),
                selection=None,
            )
        except ToolCatalogConflictError as exc:
            raise _tool_loop_config_error(
                config_path,
                "tool_allowlist does not match the trusted ToolRegistry catalog",
                field_path="tool_allowlist",
            ) from exc
    return catalogs


def _tool_loop_config_error(
    config_path: Path,
    message: str,
    *,
    field_path: str = "model_tool_loop",
) -> RegistryLoadError:
    """生成不暴露 deployment 私有配置的稳定 Registry 诊断。"""

    return RegistryLoadError(
        [
            ErrorDetail(
                code="registry.invalid_config",
                message=message,
                field_path=field_path,
                hint=f"修正 agent config：{config_path}",
            )
        ]
    )


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


def _load_descriptor_configs(
    root: Path,
) -> list[tuple[AgentConfigRecord, Path, str]]:
    """仅解析 registry 配置数据，并在接触 import 或运行时协作者前拒绝脏集合。"""

    loaded: list[tuple[AgentConfigRecord, Path, str]] = []
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
        loaded.append((config, config_path, executor_ref))
    return loaded


class AgentRegistry:
    """从模板 agent config 构造的只读 registry。"""

    def __init__(
        self,
        descriptors: Sequence[AgentDescriptor],
        *,
        executors: Mapping[str, AgentExecutor] | None = None,
        output_schemas: Mapping[str, OutputSchemaDefinition] | None = None,
        tool_catalogs: Mapping[str, ToolCatalog] | None = None,
    ) -> None:
        """以已验证的 descriptor 和 executor 构建只读索引。

        该构造器不执行文件加载或动态 import，便于测试和 composition root 注入；
        从磁盘构建时应使用 ``load_from_directory`` 完成全量验证后再调用这里。
        """

        self._descriptors = {descriptor.agent_id: descriptor for descriptor in descriptors}
        self._executors = dict(executors or {})
        self._output_schemas = dict(output_schemas or {})
        self._tool_catalogs = {
            agent_id: catalog.model_copy(deep=True)
            for agent_id, catalog in (tool_catalogs or {}).items()
        }

    @classmethod
    def load_from_directory(
        cls,
        root: Path,
        *,
        model_settings: ModelSettings | None = None,
        tool_catalog_descriptors: tuple[ToolCatalogSourceDescriptor, ...] | None = None,
    ) -> AgentRegistry:
        """从受控目录加载所有 agent config，并拒绝部分可用的脏 registry。"""

        loaded_configs = _load_descriptor_configs(root)
        descriptor_configs = [
            (config, config_path) for config, config_path, _executor_ref in loaded_configs
        ]
        executor_refs = [
            (config.agent_id, executor_ref, config_path)
            for config, config_path, executor_ref in loaded_configs
        ]
        schema_refs: list[tuple[str, str, str, Path]] = []
        for config, config_path, _executor_ref in loaded_configs:
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

        _validate_tool_loop_capabilities(descriptor_configs, model_settings=model_settings)
        tool_catalogs = _build_agent_tool_catalogs(
            descriptor_configs,
            tool_catalog_descriptors=tool_catalog_descriptors,
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
        return cls(
            descriptors,
            executors=executors,
            output_schemas=output_schemas,
            tool_catalogs=tool_catalogs,
        )

    @classmethod
    def require_declared_agent(cls, root: Path, agent_id: str) -> None:
        """以 data-only 配置探针确认目标存在，不触发 import、恢复或存储初始化。

        探针仍校验整个 YAML 集合与重复 ``agent_id``，避免为了提前返回未知目标而把
        脏 registry 伪装成可用；完整 schema、tool catalog 与 executor 校验仍由
        ``load_from_directory`` 在运行时协作者建立后统一完成。
        """

        if any(
            config.agent_id == agent_id for config, _path, _ref in _load_descriptor_configs(root)
        ):
            return
        raise RegistryLoadError(
            [
                ErrorDetail(
                    code="registry.agent_not_found",
                    message=f"agent not found: {agent_id}",
                    field_path="agent_id",
                )
            ]
        )

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

    def resolve_tool_catalog(
        self,
        agent_id: str,
        selection: ToolCatalogSelection | None,
    ) -> ToolCatalog:
        """从加载期冻结快照生成缺省、显式空或保序缩小的模型工具目录。"""

        self.get(agent_id)
        try:
            catalog = self._tool_catalogs[agent_id]
        except KeyError as exc:
            raise RegistryLoadError(
                [
                    ErrorDetail(
                        code="registry.tool_catalog_not_found",
                        message=f"tool catalog not found: {agent_id}",
                        field_path="tool_allowlist",
                    )
                ]
            ) from exc
        descriptors = tuple(
            ToolCatalogSourceDescriptor(
                name=item.name,
                action=item.action,
                resource=item.resource,
                input_schema=item.input_schema.model_copy(deep=True),
                registry_ordinal=item.ordinal,
            )
            for item in catalog.tools
        )
        return build_tool_catalog(
            allowed_tools=tuple(item.name for item in catalog.tools),
            registry_descriptors=descriptors,
            selection=selection,
        )

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
