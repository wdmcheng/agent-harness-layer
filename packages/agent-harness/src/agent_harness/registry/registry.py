"""多 agent registry loader 与 delegation 校验。"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import inspect
import sys
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

import yaml
from pydantic import BaseModel, Field, ValidationError
from yaml import YAMLError

from agent_harness.contracts import ErrorDetail, HarnessError
from agent_harness.contracts.dto import HarnessDTO
from agent_harness.registry.descriptor import (
    AgentBudget,
    AgentDescriptor,
    AgentModelPolicy,
    AgentToolPolicy,
)
from agent_harness.runtime.executor import AgentExecutor


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
    executor: str
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


def _load_descriptor(config_path: Path, *, root: Path) -> tuple[AgentDescriptor, str]:
    raw = _read_yaml_mapping(config_path)
    try:
        config = _AgentConfig.model_validate(raw)
    except ValidationError as exc:
        raise RegistryLoadError(_validation_errors(exc, config_path)) from exc
    # public descriptor 只能带相对 config_ref 和摘要字段，不能把本机路径或
    # provider/client/callable 暴露给 API 和 CLI 调用方。
    descriptor = AgentDescriptor(
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
    _validate_executor_reference(config.executor, config_path)
    return descriptor, config.executor


def _validate_executor_reference(reference: str, config_path: Path) -> None:
    try:
        module_ref, attribute = reference.split(":", maxsplit=1)
    except ValueError as exc:
        raise _executor_error(config_path, "executor must use '<module>:<attribute>'") from exc
    module_ref = module_ref.removesuffix(".py")
    parts = module_ref.replace("/", ".").split(".")
    if (
        not module_ref
        or not attribute.isidentifier()
        or any(not part.isidentifier() for part in parts)
        or Path(module_ref).is_absolute()
        or ".." in reference
    ):
        raise _executor_error(config_path, "executor reference must stay inside the agent package")


def _resolve_executor_target(reference: str, config_path: Path) -> tuple[Path, str]:
    module_ref, attribute = reference.split(":", maxsplit=1)
    package_root = config_path.parent.resolve()
    module_parts = module_ref.removesuffix(".py").replace("/", ".").split(".")
    module_path = package_root.joinpath(*module_parts).with_suffix(".py").resolve()
    if not module_path.is_relative_to(package_root):
        raise _executor_error(config_path, "executor module escapes the agent package")
    if not module_path.is_file():
        raise _executor_error(config_path, f"executor module is missing: {module_ref}")
    return module_path, attribute


def _load_executor(agent_id: str, module_path: Path, attribute: str) -> AgentExecutor:
    module_key = sha256(str(module_path).encode()).hexdigest()[:16]
    module_name = f"_agent_harness_executor_{module_key}"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise _executor_error(module_path, "executor module cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
        target = getattr(module, attribute)
        executor = target() if inspect.isclass(target) else target
    except Exception as exc:  # noqa: BLE001 - import failures become stable registry errors
        sys.modules.pop(module_name, None)
        raise _executor_error(module_path, f"executor import failed for {agent_id}: {exc}") from exc
    if not isinstance(executor, AgentExecutor):
        raise _executor_error(module_path, "executor object must implement async run and resume")
    if not inspect.iscoroutinefunction(executor.run) or not inspect.iscoroutinefunction(
        executor.resume
    ):
        raise _executor_error(module_path, "executor run and resume must be async callables")
    return executor


def _resolve_schema_target(
    reference: str,
    config_path: Path,
    *,
    root: Path,
    field_path: str,
) -> tuple[str, str]:
    """把公开 dotted schema ref 约束到当前 registry root。"""

    module_ref, separator, attribute = reference.rpartition(".")
    module_parts = module_ref.split(".")
    if (
        separator != "."
        or not module_ref
        or not attribute.isidentifier()
        or not module_parts
        or module_parts[0] != root.resolve().name
        or any(not part.isidentifier() for part in module_parts[1:])
    ):
        raise _schema_error(
            config_path,
            field_path,
            "schema reference must use '<agents-root>.<package>.<module>.<attribute>'",
        )
    module_base = root.resolve().parent.joinpath(*module_parts).resolve()
    if not module_base.is_relative_to(root.resolve()):
        raise _schema_error(config_path, field_path, "schema module escapes the agents root")
    module_path = module_base.with_suffix(".py")
    package_path = module_base / "__init__.py"
    if not module_path.is_file() and not package_path.is_file():
        raise _schema_error(config_path, field_path, f"schema module is missing: {module_ref}")
    return module_ref, attribute


def _load_schema(
    agent_id: str,
    module_ref: str,
    attribute: str,
    *,
    field_path: str,
) -> type[BaseModel]:
    """加载并验证 schema 对象，executor 只会在全部 schema 合法后导入。"""

    try:
        module = __import__(module_ref, fromlist=[attribute])
        target = getattr(module, attribute)
    except Exception as exc:  # noqa: BLE001 - import failures become stable registry errors
        raise _schema_error(
            Path(module_ref),
            field_path,
            f"schema import failed for {agent_id}: {exc}",
        ) from exc
    if not isinstance(target, type) or not issubclass(target, BaseModel):
        raise _schema_error(
            Path(module_ref),
            field_path,
            "schema object must be a Pydantic BaseModel type",
        )
    return target


@contextmanager
def _agent_import_context(root: Path) -> Generator[None]:
    """隔离一个 registry root 的 package import，并在加载后恢复进程状态。

    具体 Agent 可以导入同一 `agents_dir` 下的共享模块，但不能碰巧复用进程里
    另一份同名 package。CLI 传绝对 `--agents-dir` 时也不依赖调用方 cwd。
    """

    resolved_root = root.resolve()
    namespace = resolved_root.name
    import_root = str(resolved_root.parent)
    prefix = f"{namespace}."
    saved_modules = {
        name: module
        for name, module in tuple(sys.modules.items())
        if name == namespace or name.startswith(prefix)
    }
    for name in saved_modules:
        sys.modules.pop(name, None)
    sys.path.insert(0, import_root)
    importlib.invalidate_caches()
    if not (resolved_root / "__init__.py").is_file():
        namespace_spec = importlib.machinery.ModuleSpec(
            namespace,
            loader=None,
            is_package=True,
        )
        namespace_spec.submodule_search_locations = [str(resolved_root)]
        sys.modules[namespace] = importlib.util.module_from_spec(namespace_spec)
    try:
        yield
    finally:
        for name in tuple(sys.modules):
            if name == namespace or name.startswith(prefix):
                sys.modules.pop(name, None)
        sys.modules.update(saved_modules)
        try:
            sys.path.remove(import_root)
        except ValueError:
            pass


def _executor_error(path: Path, message: str) -> RegistryLoadError:
    return RegistryLoadError(
        [
            ErrorDetail(
                code="registry.invalid_executor",
                message=message,
                field_path="executor",
                hint=f"修正 agent executor：{path}",
            )
        ]
    )


def _schema_error(path: Path, field_path: str, message: str) -> RegistryLoadError:
    return RegistryLoadError(
        [
            ErrorDetail(
                code="registry.invalid_schema",
                message=message,
                field_path=field_path,
                hint=f"修正 agent schema reference：{path}",
            )
        ]
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
