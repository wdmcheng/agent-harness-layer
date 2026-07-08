"""多 agent registry loader 与 delegation 校验。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

import yaml
from pydantic import Field, ValidationError
from yaml import YAMLError

from agent_harness.contracts import ErrorDetail, HarnessError
from agent_harness.contracts.dto import HarnessDTO
from agent_harness.registry.descriptor import (
    AgentBudget,
    AgentDescriptor,
    AgentModelPolicy,
    AgentToolPolicy,
)


class RegistryLoadError(HarnessError):
    """registry 配置加载失败，携带稳定错误详情。"""


class _AgentModelConfig(HarnessDTO):
    provider: str
    default_model: str
    fallback_models: list[str] = Field(default_factory=list)


class _AgentBudgetConfig(HarnessDTO):
    max_tokens_per_run: int
    max_cost_usd_per_run: float | None


class _AgentConfig(HarnessDTO):
    agent_id: str
    version: str
    name: str
    description: str
    input_schema: str
    output_schema: str
    model: _AgentModelConfig
    budget: _AgentBudgetConfig
    tool_allowlist: list[str] = Field(default_factory=list)
    eval_dataset: str | None = None
    delegation_edges: list[str] = Field(default_factory=list)


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

    def __init__(self, descriptors: Sequence[AgentDescriptor]) -> None:
        self._descriptors = {descriptor.agent_id: descriptor for descriptor in descriptors}

    @classmethod
    def load_from_directory(cls, root: Path) -> AgentRegistry:
        """从受控目录加载所有 agent config，并拒绝部分可用的脏 registry。"""

        descriptors: list[AgentDescriptor] = []
        seen: dict[str, Path] = {}
        for config_path in sorted(root.rglob("config.yaml")):
            descriptor = _load_descriptor(config_path, root=root)
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
        return cls(descriptors)

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


def _load_descriptor(config_path: Path, *, root: Path) -> AgentDescriptor:
    raw = _read_yaml_mapping(config_path)
    try:
        config = _AgentConfig.model_validate(raw)
    except ValidationError as exc:
        raise RegistryLoadError(_validation_errors(exc, config_path)) from exc
    # public descriptor 只能带相对 config_ref 和摘要字段，不能把本机路径或
    # provider/client/callable 暴露给 API 和 CLI 调用方。
    return AgentDescriptor(
        agent_id=config.agent_id,
        version=config.version,
        name=config.name,
        description=config.description,
        input_schema_ref=config.input_schema,
        output_schema_ref=config.output_schema,
        config_ref=config_path.relative_to(root).as_posix(),
        tool_policy=AgentToolPolicy(allowed_tools=config.tool_allowlist),
        model_policy=AgentModelPolicy(
            provider=config.model.provider,
            default_model=config.model.default_model,
            fallback_models=config.model.fallback_models,
        ),
        budget=AgentBudget(
            max_tokens_per_run=config.budget.max_tokens_per_run,
            max_cost_usd_per_run=config.budget.max_cost_usd_per_run,
        ),
        eval_dataset=config.eval_dataset,
        delegation_targets=config.delegation_edges,
    )


def _read_yaml_mapping(path: Path) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except YAMLError as exc:
        raise RegistryLoadError(
            [
                ErrorDetail(
                    code="registry.invalid_config",
                    message=f"YAML 解析失败：{exc}",
                    field_path=str(path),
                )
            ]
        ) from exc
    if not isinstance(raw, dict):
        raise RegistryLoadError(
            [
                ErrorDetail(
                    code="registry.invalid_config",
                    message="agent config 必须是 mapping",
                    field_path=str(path),
                )
            ]
        )
    return cast(dict[str, Any], raw)


def _validation_errors(exc: ValidationError, config_path: Path) -> list[ErrorDetail]:
    errors: list[ErrorDetail] = []
    for item in exc.errors():
        loc = item.get("loc", ())
        field_path = ".".join(str(part) for part in loc) if loc else str(config_path)
        errors.append(
            ErrorDetail(
                code="registry.invalid_config",
                message=str(item.get("msg", "agent config 校验失败")),
                field_path=field_path,
                hint=f"修正 agent config：{config_path}",
            )
        )
    return errors
