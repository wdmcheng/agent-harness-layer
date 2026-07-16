"""安全生成可由当前 registry/runtime 加载的 Agent package。"""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path
from uuid import uuid4

from agent_harness._scaffold_support import (
    ExecutorRollbackInventory as ExecutorRollbackInventory,
)
from agent_harness._scaffold_support import (
    ScaffoldError as ScaffoldError,
)
from agent_harness._scaffold_support import (
    ScaffoldResult as ScaffoldResult,
)
from agent_harness._scaffold_support import (
    acquire_publish_lock as _acquire_publish_lock,
)
from agent_harness._scaffold_support import (
    cleanup_package_parents as _cleanup_package_parents,
)
from agent_harness._scaffold_support import discover_agents_dir as discover_agents_dir
from agent_harness._scaffold_support import (
    executor_contracts as _executor_contracts,
)
from agent_harness._scaffold_support import (
    prepare_package_parents as _prepare_package_parents,
)
from agent_harness._scaffold_support import (
    remove_generated_bytecode as _remove_generated_bytecode,
)
from agent_harness._scaffold_support import (
    resolve_agents_dir as _resolve_agents_dir,
)
from agent_harness._scaffold_support import (
    validate_generated_package as _validate_generated_package,
)
from agent_harness._scaffold_support import (
    validate_target_path as _validate_target_path,
)
from agent_harness.scaffold_templates import render_staged_package

# 公开对象仍以原 facade 为身份，避免职责拆分改变文档、序列化或诊断输出。
ExecutorRollbackInventory.__module__ = __name__
ScaffoldError.__module__ = __name__
ScaffoldResult.__module__ = __name__
discover_agents_dir.__module__ = __name__

_AGENT_ID = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*")
_STAGING_NAME = ".agent-harness-scaffold-staging"
_LOCK_NAME = ".agent-harness-scaffold.lock"


def validate_agent_id(agent_id: str) -> tuple[str, ...]:
    """只接受点分的小写 Python identifier，避免路径与 import 歧义。"""

    if _AGENT_ID.fullmatch(agent_id) is None:
        raise ScaffoldError(
            "scaffold.invalid_agent_id",
            f"invalid agent_id: {agent_id}",
            hint="使用点分小写 Python identifier，例如 support.triage",
        )
    return tuple(agent_id.split("."))


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


__all__ = [
    "ExecutorRollbackInventory",
    "ScaffoldError",
    "ScaffoldResult",
    "discover_agents_dir",
    "executor_rollback_preflight",
    "scaffold_agent_package",
    "validate_agent_id",
]
