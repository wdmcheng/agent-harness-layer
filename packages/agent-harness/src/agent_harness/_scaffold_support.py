"""Scaffold 类型、路径验证、回滚与原子发布辅助实现。"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import cast
from uuid import uuid4

import yaml

from agent_harness.contracts.dto import HarnessDTO
from agent_harness.registry import AgentRegistry
from agent_harness.scaffold_templates import PACKAGE_INIT


class ScaffoldError(RuntimeError):
    """脚手架操作的稳定失败边界，供 CLI 映射为非零退出。"""

    def __init__(self, code: str, message: str, *, hint: str | None = None) -> None:
        """保存机器可判定的错误码与可选修复提示，不在此处执行输出格式化。"""

        self.code = code
        self.message = message
        self.hint = hint
        super().__init__(message)


@dataclass(frozen=True)
class ScaffoldResult:
    """成功发布后的 agent 路径摘要，避免 CLI 依赖绝对工作区路径。"""

    agent_id: str
    agents_dir: Path
    target_dir: Path

    @property
    def relative_path(self) -> str:
        """返回相对 agents root 的稳定展示路径，供 CLI 和测试显示发布结果。"""

        return self.target_dir.relative_to(self.agents_dir).as_posix()


@dataclass(frozen=True)
class ExecutorRollbackInventory:
    """移除 executor seam 前的只读兼容性盘点。

    盘点结果区分仍在使用的 agent 与会因移除而失去兼容性的 agent；调用方必须依据
    ``allowed`` 决定是否继续，不能在写入后才发现已有配置不可加载。
    """

    active_agent_ids: tuple[str, ...]
    incompatible_agent_ids: tuple[str, ...]

    @property
    def allowed(self) -> bool:
        """只有没有不兼容 agent 时才允许执行移除操作。"""

        return not self.incompatible_agent_ids


def discover_agents_dir(*, cwd: Path | None = None) -> Path:
    """从 service-app 或受控源 workspace 标记发现唯一 agents root。"""

    start = (cwd or Path.cwd()).resolve()
    candidates = (start, *start.parents)
    service_roots = [path for path in candidates if is_service_app_root(path)]
    if len(service_roots) == 1:
        return service_roots[0] / "agents"
    if len(service_roots) > 1:
        raise ScaffoldError(
            "scaffold.agents_dir_required",
            "multiple service-app roots were discovered",
            hint="传入 --agents-dir 消除歧义",
        )
    workspace_roots = [path for path in candidates if is_source_workspace_root(path)]
    if len(workspace_roots) == 1:
        return workspace_roots[0] / "templates" / "service-app" / "agents"
    raise ScaffoldError(
        "scaffold.agents_dir_required",
        "unable to discover a unique service-app agents directory",
        hint="传入 --agents-dir",
    )


def resolve_agents_dir(agents_dir: Path | None, *, cwd: Path | None) -> Path:
    """解析显式或自动发现的 agents root，并拒绝入口本身为符号链接的路径。"""

    if agents_dir is None:
        return discover_agents_dir(cwd=cwd)
    expanded = agents_dir.expanduser()
    if expanded.is_symlink():
        raise ScaffoldError(
            "scaffold.symlink_escape",
            f"agents_dir must not be a symlink: {expanded}",
        )
    parent = expanded.parent.resolve()
    # 保留最后一个路径名而只解析父目录，避免不存在目标被 resolve 成意外的替代位置。
    return parent / expanded.name


def executor_contracts(agents_dir: Path) -> dict[str, str]:
    """只读 config 盘点 agent/executor 绑定，避免 import 产生副作用。"""

    contracts: dict[str, str] = {}
    for config_path in sorted(agents_dir.rglob("config.yaml")):
        try:
            raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise ScaffoldError(
                "scaffold.executor_inventory_failed",
                f"cannot read agent config during rollback preflight: {config_path}",
            ) from exc
        if not isinstance(raw, dict):
            raise ScaffoldError(
                "scaffold.executor_inventory_failed",
                f"agent config must be a mapping: {config_path}",
            )
        config = cast(dict[object, object], raw)
        agent_id = config.get("agent_id")
        executor = config.get("executor")
        if not isinstance(agent_id, str) or not isinstance(executor, str) or not executor:
            raise ScaffoldError(
                "scaffold.executor_inventory_failed",
                f"agent config lacks an explicit executor contract: {config_path}",
            )
        existing = contracts.get(agent_id)
        if existing is not None and existing != executor:
            raise ScaffoldError(
                "scaffold.executor_inventory_failed",
                f"agent_id has conflicting executor contracts: {agent_id}",
            )
        contracts[agent_id] = executor
    return contracts


def validate_target_path(root: Path, parts: Sequence[str]) -> None:
    """验证生成目标及其父级都位于真实 agents root 内且尚未存在。

    脚手架从不覆盖、合并或沿符号链接写入既有目录；这些约束确保生成过程不能通过
    预置路径跳出目标根目录，也不能误伤已有 agent 包。
    """

    root_resolved = root.resolve(strict=False)
    if root.exists() and (not root.is_dir() or root.is_symlink()):
        raise ScaffoldError(
            "scaffold.invalid_agents_dir",
            f"agents_dir must be a real directory: {root}",
        )
    current = root
    for part in parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise ScaffoldError(
                "scaffold.symlink_escape",
                f"agent parent must not be a symlink: {current}",
            )
        if current.exists() and not current.is_dir():
            raise ScaffoldError(
                "scaffold.invalid_target_parent",
                f"agent parent is not a directory: {current}",
            )
        if current.exists() and not current.resolve().is_relative_to(root_resolved):
            raise ScaffoldError(
                "scaffold.symlink_escape",
                f"agent parent escapes agents_dir: {current}",
            )
    target = root.joinpath(*parts)
    # 最终目标即使是悬挂符号链接也视为已占用，不能把生成内容导向其指向位置。
    if target.exists() or target.is_symlink():
        raise ScaffoldError(
            "scaffold.target_exists",
            f"agent target already exists: {target}",
            hint="scaffold 不提供 --force，也不会合并已有目录",
        )


def validate_generated_package(agents_root: Path, target: Path, agent_id: str) -> None:
    """从实际 registry 重新加载生成包，验证路径、schema、模型与权限防护没有漂移。

    仅检查渲染文件内容不足以证明运行时能加载它；这里通过正式 registry 走一遍解析，
    同时锁住离线 fake 模型、无工具和无委派的默认安全承诺。
    """

    registry = AgentRegistry.load_from_directory(agents_root)
    descriptor = registry.get(agent_id)
    registry.resolve_executor(agent_id)
    schema_prefix = f"{agents_root.name}.{agent_id}.schemas"
    if descriptor.input_schema_ref != f"{schema_prefix}.ScaffoldInput":
        raise ValueError("scaffold input schema ref does not match the selected agents_dir")
    if descriptor.output_schema_ref != f"{schema_prefix}.ScaffoldOutput":
        raise ValueError("scaffold output schema ref does not match the selected agents_dir")
    expected_eval = "/".join((agents_root.name, *agent_id.split("."), "evals", "approved"))
    if descriptor.eval_dataset != expected_eval:
        raise ValueError("scaffold eval dataset ref does not match the selected agents_dir")
    if descriptor.model_policy.provider != "fake":
        raise ValueError("scaffold model provider must remain fake")
    if descriptor.tool_policy.allowed_tools or descriptor.delegation_targets:
        raise ValueError("scaffold must not grant tools or delegation")
    module = load_schema_module(target / "schemas.py")
    for name in ("ScaffoldInput", "ScaffoldOutput"):
        schema = getattr(module, name, None)
        if not isinstance(schema, type) or not issubclass(schema, HarnessDTO):
            raise ValueError(f"generated schema is invalid: {name}")


def load_schema_module(path: Path) -> ModuleType:
    """临时加载刚生成的 schema 模块进行类型验证，并在结束后清理模块缓存。"""

    module_name = f"_agent_harness_scaffold_schema_{uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"generated schema cannot be loaded: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        # 生成验证不应污染后续 registry 加载；随机模块名也避免并发验证相互覆盖。
        sys.modules.pop(module_name, None)
    return module


def prepare_package_parents(
    root: Path,
    parts: Sequence[str],
    *,
    created_dirs: list[Path],
    created_files: list[Path],
) -> None:
    """在暂存区创建缺失的命名空间父目录与 package 初始化文件，并记录可回滚产物。"""

    if not root.exists():
        root.mkdir()
        created_dirs.append(root)
    current = root
    for part in parts[:-1]:
        current /= part
        if not current.exists():
            current.mkdir()
            created_dirs.append(current)
    for package_dir in (root, *(root.joinpath(*parts[:index]) for index in range(1, len(parts)))):
        init_path = package_dir / "__init__.py"
        if not init_path.exists():
            # 仅记录本次新建的文件，回滚时不会删除调用方原有的 package 边界。
            write_text(init_path, PACKAGE_INIT)
            created_files.append(init_path)


def remove_generated_bytecode(target: Path) -> None:
    """验证可 import 后移除解释器缓存，避免把机器相关文件发布到生成目录。"""

    for cache_dir in target.rglob("__pycache__"):
        shutil.rmtree(cache_dir, ignore_errors=True)


def cleanup_package_parents(files: Sequence[Path], directories: Sequence[Path]) -> None:
    """按逆创建顺序清理本轮新增 package 文件和空目录，不触碰预先存在的内容。"""

    for path in reversed(files):
        path.unlink(missing_ok=True)
    for path in reversed(directories):
        try:
            path.rmdir()
        except OSError:
            # 目录包含调用方或并发操作留下的内容时保留它，避免失败清理扩大影响面。
            pass


def acquire_publish_lock(lock_path: Path) -> int:
    """原子创建权限最小的发布锁文件，阻止两个脚手架进程同时发布同一路径。"""

    try:
        return os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise ScaffoldError(
            "scaffold.concurrent_operation",
            f"another scaffold publish is active: {lock_path}",
        ) from exc


def write_text(path: Path, content: str) -> None:
    """写入暂存生成文件；调用方需先完成目标路径和发布锁验证。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def load_pyproject(path: Path) -> Mapping[str, object] | None:
    """宽容读取候选根目录的 pyproject；不可读或无效 TOML 仅表示它不是已知根。"""

    try:
        with path.open("rb") as file:
            return tomllib.load(file)
    except (OSError, tomllib.TOMLDecodeError):
        return None


def as_mapping(value: object) -> Mapping[str, object] | None:
    """将 TOML 根节点安全收窄为映射，避免后续结构检查依赖不受信任的动态类型。"""

    if not isinstance(value, dict):
        return None
    return cast(Mapping[str, object], value)


def is_service_app_root(path: Path) -> bool:
    """判断目录是否是可直接承载生成 agent 的 service-app 根目录。"""

    config = load_pyproject(path / "pyproject.toml")
    if config is None or not (path / "agents").is_dir():
        return False
    project = as_mapping(config.get("project"))
    return project is not None and project.get("name") == "agent-harness-service-app"


def is_source_workspace_root(path: Path) -> bool:
    """判断目录是否是包含 service-app 模板成员的受控源码工作区。"""

    config = load_pyproject(path / "pyproject.toml")
    agents_dir = path / "templates" / "service-app" / "agents"
    if config is None or not agents_dir.is_dir():
        return False
    project = as_mapping(config.get("project"))
    tool = as_mapping(config.get("tool"))
    if project is None or project.get("name") != "agent-harness-layer":
        return False
    if tool is None:
        return False
    uv_config = as_mapping(tool.get("uv"))
    if uv_config is None:
        return False
    workspace = as_mapping(uv_config.get("workspace"))
    raw_members = workspace.get("members") if workspace is not None else None
    members = cast(list[object], raw_members) if isinstance(raw_members, list) else []
    # 同时检查项目名和 workspace 成员，避免普通 pyproject 被误识别为源码根。
    return "templates/service-app" in members


__all__ = [
    "ExecutorRollbackInventory",
    "discover_agents_dir",
    "ScaffoldError",
    "ScaffoldResult",
    "resolve_agents_dir",
    "executor_contracts",
    "validate_target_path",
    "validate_generated_package",
    "load_schema_module",
    "prepare_package_parents",
    "remove_generated_bytecode",
    "cleanup_package_parents",
    "acquire_publish_lock",
    "write_text",
    "load_pyproject",
    "as_mapping",
    "is_service_app_root",
    "is_source_workspace_root",
]
