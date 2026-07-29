"""Agent registry 的 descriptor、schema 与 executor 加载实现。"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import inspect
import sys
from collections.abc import Generator
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
    """配置文件中模型选择的最小结构；仅在加载时存在，不作为公开 registry 描述符。"""

    deployment_id: str = "fake_default"
    provider: str
    allowed_models: list[str] = Field(default_factory=list)
    default_model: str
    fallback_models: list[str] = Field(default_factory=list)


class _AgentBudgetConfig(HarnessDTO):
    """配置文件中单次 agent 预算上限的最小结构，供公开 descriptor 安全投影。"""

    max_tokens_per_run: int
    max_cost_usd_per_run: float | None


class _AgentConfig(HarnessDTO):
    """从单个 agent YAML 解析的内部完整配置，包含公开描述符和本地导入坐标。"""

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


def load_descriptor(config_path: Path, *, root: Path) -> tuple[AgentDescriptor, str]:
    """读取并校验 agent YAML，返回脱敏公开描述符与私有 executor 引用。

    描述符只承载 API/CLI 可以安全显示的字段；executor 引用在返回前单独校验，但不被
    放进公开 DTO，防止本地模块结构或可调用对象进入外部边界。
    """

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
            deployment_id=config.model.deployment_id,
            provider=config.model.provider,
            allowed_models=config.model.allowed_models,
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
    """校验 executor 使用受限的 ``module:attribute`` 形态且不能借路径片段逃出包根。"""

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


def resolve_executor_target(reference: str, config_path: Path) -> tuple[Path, str]:
    """在 agent 包根内解析已校验 executor 引用，并要求目标模块是普通存在的 Python 文件。"""

    module_ref, attribute = reference.split(":", maxsplit=1)
    package_root = config_path.parent.resolve()
    module_parts = module_ref.removesuffix(".py").replace("/", ".").split(".")
    module_path = package_root.joinpath(*module_parts).with_suffix(".py").resolve()
    if not module_path.is_relative_to(package_root):
        raise _executor_error(config_path, "executor module escapes the agent package")
    if not module_path.is_file():
        raise _executor_error(config_path, f"executor module is missing: {module_ref}")
    return module_path, attribute


def load_executor(agent_id: str, module_path: Path, attribute: str) -> AgentExecutor:
    """隔离导入 executor 模块并验证其实现异步 ``run`` 与 ``resume`` 协议。

    使用路径摘要作为临时模块名，避免不同 agent 包里同名模块污染 ``sys.modules``；
    导入失败统一变为 registry 错误，调用方不需要处理任意 import 异常细节。
    """

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


def resolve_schema_target(
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


def load_schema(
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
def agent_import_context(root: Path) -> Generator[None]:
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
    """构造 executor 字段的稳定加载错误，并给维护者保留最小修复坐标。"""

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
    """构造 schema 引用的稳定加载错误，不向外暴露导入过程的内部对象。"""

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
    """读取配置根映射并把 YAML 语法或结构错误收敛为可展示的 registry 错误。"""

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
    """把 Pydantic 字段错误转换为稳定、可定位的配置诊断列表。"""

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


__all__ = [
    "RegistryLoadError",
    "agent_import_context",
    "load_descriptor",
    "load_executor",
    "load_schema",
    "resolve_executor_target",
    "resolve_schema_target",
]
