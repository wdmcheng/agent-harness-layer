"""多 agent registry loader 与 delegation 校验。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic import Field

from agent_harness.contracts import ErrorDetail
from agent_harness.contracts.dto import HarnessDTO
from agent_harness.registry._loader import (
    RegistryLoadError as RegistryLoadError,
)
from agent_harness.registry._loader import agent_import_context as _agent_import_context
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
    ) -> None:
        self._descriptors = {descriptor.agent_id: descriptor for descriptor in descriptors}
        self._executors = dict(executors or {})

    @classmethod
    def load_from_directory(cls, root: Path) -> AgentRegistry:
        """从受控目录加载所有 agent config，并拒绝部分可用的脏 registry。"""

        descriptors: list[AgentDescriptor] = []
        executor_refs: list[tuple[str, str, Path]] = []
        schema_refs: list[tuple[str, str, str, Path]] = []
        seen: dict[str, Path] = {}
        for config_path in sorted(root.rglob("config.yaml")):
            descriptor, executor_ref = _load_descriptor(config_path, root=root)
            first_seen = seen.get(descriptor.agent_id)
            if first_seen is not None:
                raise RegistryLoadError(
                    [
                        ErrorDetail(
                            code="registry.duplicate_agent_id",
                            message=f"duplicate agent_id: {descriptor.agent_id}",
                            field_path="agent_id",
                            hint=f"检查 {first_seen} 和 {config_path}",
                        )
                    ]
                )
            seen[descriptor.agent_id] = config_path
            descriptors.append(descriptor)
            executor_refs.append((descriptor.agent_id, executor_ref, config_path))
            schema_refs.extend(
                [
                    (
                        descriptor.agent_id,
                        "input_schema",
                        descriptor.input_schema_ref,
                        config_path,
                    ),
                    (
                        descriptor.agent_id,
                        "output_schema",
                        descriptor.output_schema_ref,
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
        with _agent_import_context(root):
            for agent_id, field_path, (module_ref, attribute) in resolved_schemas:
                _load_schema(agent_id, module_ref, attribute, field_path=field_path)
            executors = {
                agent_id: _load_executor(agent_id, module_path, attribute)
                for agent_id, (module_path, attribute) in resolved_targets
            }
        return cls(descriptors, executors=executors)

    def list_agents(self) -> list[AgentDescriptor]:
        return sorted(self._descriptors.values(), key=lambda item: item.agent_id)

    def get(self, agent_id: str) -> AgentDescriptor:
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

    def check_delegation(self, source_agent_id: str, target_agent_id: str) -> DelegationDecision:
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
