"""安全生成可由当前 registry/runtime 加载的 Agent package。"""

from __future__ import annotations

import importlib.util
import os
import re
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
from agent_harness.scaffold_templates import PACKAGE_INIT, render_staged_package

_AGENT_ID = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*")
_STAGING_NAME = ".agent-harness-scaffold-staging"
_LOCK_NAME = ".agent-harness-scaffold.lock"


class ScaffoldError(RuntimeError):
    """scaffold 的稳定失败边界，供 CLI 映射为非零退出。"""

    def __init__(self, code: str, message: str, *, hint: str | None = None) -> None:
        self.code = code
        self.message = message
        self.hint = hint
        super().__init__(message)


@dataclass(frozen=True)
class ScaffoldResult:
    """成功发布后的 agent 路径摘要。"""

    agent_id: str
    agents_dir: Path
    target_dir: Path

    @property
    def relative_path(self) -> str:
        return self.target_dir.relative_to(self.agents_dir).as_posix()


@dataclass(frozen=True)
class ExecutorRollbackInventory:
    """移除 executor seam 前的只读兼容性盘点。"""

    active_agent_ids: tuple[str, ...]
    incompatible_agent_ids: tuple[str, ...]

    @property
    def allowed(self) -> bool:
        return not self.incompatible_agent_ids


def validate_agent_id(agent_id: str) -> tuple[str, ...]:
    """只接受点分的小写 Python identifier，避免路径与 import 歧义。"""

    if _AGENT_ID.fullmatch(agent_id) is None:
        raise ScaffoldError(
            "scaffold.invalid_agent_id",
            f"invalid agent_id: {agent_id}",
            hint="使用点分小写 Python identifier，例如 support.triage",
        )
    return tuple(agent_id.split("."))


def discover_agents_dir(*, cwd: Path | None = None) -> Path:
    """从 service-app 或受控源 workspace 标记发现唯一 agents root。"""

    start = (cwd or Path.cwd()).resolve()
    candidates = (start, *start.parents)
    service_roots = [path for path in candidates if _is_service_app_root(path)]
    if len(service_roots) == 1:
        return service_roots[0] / "agents"
    if len(service_roots) > 1:
        raise ScaffoldError(
            "scaffold.agents_dir_required",
            "multiple service-app roots were discovered",
            hint="传入 --agents-dir 消除歧义",
        )
    workspace_roots = [path for path in candidates if _is_source_workspace_root(path)]
    if len(workspace_roots) == 1:
        return workspace_roots[0] / "templates" / "service-app" / "agents"
    raise ScaffoldError(
        "scaffold.agents_dir_required",
        "unable to discover a unique service-app agents directory",
        hint="传入 --agents-dir",
    )


def scaffold_agent_package(
    agent_id: str,
    *,
    agents_dir: Path | None = None,
    cwd: Path | None = None,
) -> ScaffoldResult:
    """在扫描根外完成渲染和验证，再用一次 rename 发布具体 agent 目录。"""

    parts = validate_agent_id(agent_id)
    root = _resolve_agents_dir(agents_dir, cwd=cwd)
    target = root.joinpath(*parts)
    _validate_target_path(root, parts)

    root_parent = root.parent
    if not root_parent.is_dir():
        raise ScaffoldError(
            "scaffold.invalid_agents_dir",
            f"agents_dir parent does not exist: {root_parent}",
            hint="先创建 service-app root，或传入已有目录下的 --agents-dir",
        )
    staging_namespace = root_parent / _STAGING_NAME
    if staging_namespace.is_symlink():
        raise ScaffoldError(
            "scaffold.symlink_escape",
            f"staging namespace must not be a symlink: {staging_namespace}",
        )

    staging_instance = staging_namespace / str(uuid4())
    # staging 使用与最终 root 相同的 package basename，确保发布前验证覆盖
    # 自定义 --agents-dir 生成的真实 schema/eval 引用，而不是固定假装为 agents。
    staged_agents = staging_instance / root.name
    staged_target = staged_agents.joinpath(*parts)
    published = False
    created_package_files: list[Path] = []
    created_package_dirs: list[Path] = []
    try:
        render_staged_package(staged_agents, staged_target, agent_id, parts)
        if root_parent.stat().st_dev != staging_instance.stat().st_dev:
            raise ScaffoldError(
                "scaffold.cross_device_staging",
                "staging and agents_dir parent must be on the same filesystem",
            )
        if root.exists() and root.stat().st_dev != staging_instance.stat().st_dev:
            raise ScaffoldError(
                "scaffold.cross_device_staging",
                "staging and agents_dir must be on the same filesystem",
            )
        _validate_generated_package(staged_agents, staged_target, agent_id)
        _remove_generated_bytecode(staged_target)

        lock_path = root_parent / _LOCK_NAME
        lock_fd = _acquire_publish_lock(lock_path)
        try:
            _validate_target_path(root, parts)
            _prepare_package_parents(
                root,
                parts,
                created_dirs=created_package_dirs,
                created_files=created_package_files,
            )
            _validate_target_path(root, parts)
            os.rename(staged_target, target)
            published = True
        finally:
            os.close(lock_fd)
            lock_path.unlink(missing_ok=True)

        # 发布后的完整 registry 还会覆盖已有 sibling；任何脏 sibling 都使本次
        # scaffold 回滚，避免向一个部分可用的 registry 继续写入。
        _validate_generated_package(root, target, agent_id)
        _remove_generated_bytecode(target)
        return ScaffoldResult(agent_id=agent_id, agents_dir=root, target_dir=target)
    except ScaffoldError:
        if published:
            shutil.rmtree(target, ignore_errors=True)
        _cleanup_package_parents(created_package_files, created_package_dirs)
        raise
    except Exception as exc:  # noqa: BLE001 - 所有发布前后错误都必须走同一原子清理
        if published:
            shutil.rmtree(target, ignore_errors=True)
        _cleanup_package_parents(created_package_files, created_package_dirs)
        raise ScaffoldError(
            "scaffold.generation_failed",
            f"agent scaffold failed: {exc}",
            hint="目标和 staging 已清理；修正错误后重试",
        ) from exc
    finally:
        shutil.rmtree(staging_instance, ignore_errors=True)
        try:
            staging_namespace.rmdir()
        except OSError:
            pass


def executor_rollback_preflight(
    agents_dirs: Sequence[Path],
    *,
    target_supported_executor_refs: Sequence[str] = (),
    isolated_agent_audit_refs: Mapping[str, str] | None = None,
) -> ExecutorRollbackInventory:
    """列出仍依赖当前 executor seam 的 agent；只读且绝不自动迁移/删除。"""

    supported_refs = set(target_supported_executor_refs)
    for agent_id, audit_ref in (isolated_agent_audit_refs or {}).items():
        if not audit_ref.strip():
            raise ScaffoldError(
                "scaffold.isolation_audit_required",
                f"isolated agent requires a non-empty audit ref: {agent_id}",
            )
    active_contracts: dict[str, set[str]] = {}
    for agents_dir in agents_dirs:
        for agent_id, executor_ref in _executor_contracts(agents_dir).items():
            active_contracts.setdefault(agent_id, set()).add(executor_ref)
    active = set(active_contracts)
    # audit ref 只能证明操作者记录过隔离动作；是否真的隔离必须由扫描结果证明。
    # 显式迁移也必须由扫描到的 config executor 与目标 runtime 支持清单共同证明，
    # 不能只凭调用方传入 agent_id 或一段审计字符串绕过回滚门禁。
    incompatible = tuple(
        sorted(
            agent_id
            for agent_id, executor_refs in active_contracts.items()
            if not executor_refs or not executor_refs.issubset(supported_refs)
        )
    )
    inventory = ExecutorRollbackInventory(
        active_agent_ids=tuple(sorted(active)),
        incompatible_agent_ids=incompatible,
    )
    if incompatible:
        raise ScaffoldError(
            "scaffold.executor_rollback_blocked",
            "executor rollback is blocked by active agents: " + ", ".join(incompatible),
            hint="保留 compatibility seam，或显式迁移/带审计地隔离这些 agent",
        )
    return inventory


def _resolve_agents_dir(agents_dir: Path | None, *, cwd: Path | None) -> Path:
    if agents_dir is None:
        return discover_agents_dir(cwd=cwd)
    expanded = agents_dir.expanduser()
    if expanded.is_symlink():
        raise ScaffoldError(
            "scaffold.symlink_escape",
            f"agents_dir must not be a symlink: {expanded}",
        )
    parent = expanded.parent.resolve()
    return parent / expanded.name


def _executor_contracts(agents_dir: Path) -> dict[str, str]:
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


def _validate_target_path(root: Path, parts: Sequence[str]) -> None:
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
    if target.exists() or target.is_symlink():
        raise ScaffoldError(
            "scaffold.target_exists",
            f"agent target already exists: {target}",
            hint="scaffold 不提供 --force，也不会合并已有目录",
        )


def _validate_generated_package(agents_root: Path, target: Path, agent_id: str) -> None:
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
    module = _load_schema_module(target / "schemas.py")
    for name in ("ScaffoldInput", "ScaffoldOutput"):
        schema = getattr(module, name, None)
        if not isinstance(schema, type) or not issubclass(schema, HarnessDTO):
            raise ValueError(f"generated schema is invalid: {name}")


def _load_schema_module(path: Path) -> ModuleType:
    module_name = f"_agent_harness_scaffold_schema_{uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"generated schema cannot be loaded: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(module_name, None)
    return module


def _prepare_package_parents(
    root: Path,
    parts: Sequence[str],
    *,
    created_dirs: list[Path],
    created_files: list[Path],
) -> None:
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
            _write_text(init_path, PACKAGE_INIT)
            created_files.append(init_path)


def _remove_generated_bytecode(target: Path) -> None:
    """验证可 import 后移除解释器缓存，避免把机器相关文件发布到生成目录。"""

    for cache_dir in target.rglob("__pycache__"):
        shutil.rmtree(cache_dir, ignore_errors=True)


def _cleanup_package_parents(files: Sequence[Path], directories: Sequence[Path]) -> None:
    for path in reversed(files):
        path.unlink(missing_ok=True)
    for path in reversed(directories):
        try:
            path.rmdir()
        except OSError:
            pass


def _acquire_publish_lock(lock_path: Path) -> int:
    try:
        return os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise ScaffoldError(
            "scaffold.concurrent_operation",
            f"another scaffold publish is active: {lock_path}",
        ) from exc


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _load_pyproject(path: Path) -> Mapping[str, object] | None:
    try:
        with path.open("rb") as file:
            return tomllib.load(file)
    except (OSError, tomllib.TOMLDecodeError):
        return None


def _as_mapping(value: object) -> Mapping[str, object] | None:
    if not isinstance(value, dict):
        return None
    return cast(Mapping[str, object], value)


def _is_service_app_root(path: Path) -> bool:
    config = _load_pyproject(path / "pyproject.toml")
    if config is None or not (path / "agents").is_dir():
        return False
    project = _as_mapping(config.get("project"))
    return project is not None and project.get("name") == "agent-harness-service-app"


def _is_source_workspace_root(path: Path) -> bool:
    config = _load_pyproject(path / "pyproject.toml")
    agents_dir = path / "templates" / "service-app" / "agents"
    if config is None or not agents_dir.is_dir():
        return False
    project = _as_mapping(config.get("project"))
    tool = _as_mapping(config.get("tool"))
    if project is None or project.get("name") != "agent-harness-layer":
        return False
    if tool is None:
        return False
    uv_config = _as_mapping(tool.get("uv"))
    if uv_config is None:
        return False
    workspace = _as_mapping(uv_config.get("workspace"))
    raw_members = workspace.get("members") if workspace is not None else None
    members = cast(list[object], raw_members) if isinstance(raw_members, list) else []
    return "templates/service-app" in members
