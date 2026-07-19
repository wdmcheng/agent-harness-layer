"""检查 workspace 包依赖方向和 vendor import 边界。

这个脚本是 `make quality` 的静态门禁：它只证明当前 Python 源码表面没有
反向依赖或未批准的 vendor SDK import，不替代运行时 sandbox、packaging
resolver 或后续 adapter contract tests。
"""

from __future__ import annotations

import ast
import sys
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import cast

ROOT = Path(__file__).resolve().parents[1]
CORE_PACKAGE = ROOT / "packages" / "agent-harness"
TEMPLATE_PACKAGE = ROOT / "templates" / "service-app"
# 质量门禁从公共 contract 读取声明，避免脚本和测试各维护一份 vendor allowlist。
sys.path.insert(0, str(CORE_PACKAGE / "src"))

from agent_harness.contracts.boundaries import (  # noqa: E402
    BANNED_VENDOR_IMPORTS,
    is_vendor_import_allowed,
)

SQLALCHEMY_SESSION_NAMES = {
    "AsyncSession",
    "Session",
    "async_sessionmaker",
    "sessionmaker",
}


def _load_pyproject(path: Path) -> dict[str, object]:
    """以二进制 TOML 模式读取项目元数据，调用方负责解释缺失字段。"""

    with path.open("rb") as file:
        return cast(dict[str, object], tomllib.load(file))


def _as_mapping(value: object) -> Mapping[str, object] | None:
    """仅接受真实字典为 mapping，避免配置类型错误在边界检查中引发属性异常。"""

    if not isinstance(value, dict):
        return None
    return cast(Mapping[str, object], value)


def _dependency_names(pyproject: Mapping[str, object]) -> set[str]:
    """提取直接依赖名，足够覆盖本仓库声明式 package boundary。

    这里不实现完整 PEP 508 解析器；目标只是发现 core package 对 template、
    examples 或 workspace package 的反向依赖。
    """

    project = _as_mapping(pyproject.get("project"))
    if project is None:
        return set()
    raw_dependencies = project.get("dependencies", [])
    if not isinstance(raw_dependencies, list):
        return set()
    dependencies = cast(list[object], raw_dependencies)
    names: set[str] = set()
    for dependency in dependencies:
        if not isinstance(dependency, str):
            continue
        normalized = dependency.split(" ", 1)[0].split("=", 1)[0].split("<", 1)[0]
        names.add(normalized.replace("_", "-").lower())
    return names


def _workspace_source_names(pyproject: Mapping[str, object]) -> set[str]:
    """读取 uv workspace source 声明，锁住 template 通过包边界依赖 core。"""

    tool = _as_mapping(pyproject.get("tool"))
    if tool is None:
        return set()
    uv_config = _as_mapping(tool.get("uv"))
    if uv_config is None:
        return set()
    sources = _as_mapping(uv_config.get("sources"))
    if sources is None:
        return set()
    names: set[str] = set()
    for name, source in sources.items():
        source_mapping = _as_mapping(source)
        if source_mapping is not None and source_mapping.get("workspace") is True:
            names.add(name.replace("_", "-").lower())
    return names


def _python_files() -> list[Path]:
    """返回会被 import boundary 扫描的源码表面。

    adapters 目录仍会被扫描；是否允许 vendor import 交给
    `agent_harness.contracts.boundaries` 的路径职责判断。
    """

    roots = [
        ROOT / "packages",
        ROOT / "templates",
        ROOT / "examples",
        ROOT / "scripts",
        ROOT / "tests",
    ]
    files: list[Path] = []
    for root in roots:
        if root.exists():
            files.extend(path for path in root.rglob("*.py") if ".venv" not in path.parts)
    return sorted(files)


def _top_level_imports(path: Path) -> set[str]:
    """用 AST 提取静态顶层 import 名。

    这是质量门禁，不是安全审计器；动态 import 和字符串执行另由后续安全门禁处理。
    """

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".", 1)[0])
    return imports


def _sqlalchemy_session_imports(path: Path) -> set[str]:
    """提取 SQLAlchemy session 相关 import 名，用于业务入口边界扫描。"""

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in {
            "sqlalchemy.orm",
            "sqlalchemy.ext.asyncio",
        }:
            names.update(
                alias.name for alias in node.names if alias.name in SQLALCHEMY_SESSION_NAMES
            )
    return names


def _is_future_adapter_path(path: Path) -> bool:
    """判断路径是否位于未来 adapter/integration seam。"""

    return is_vendor_import_allowed(path.relative_to(ROOT))


def check_core_dependencies() -> list[str]:
    """防止核心包在 metadata 层反向依赖 template 或 examples。"""

    issues: list[str] = []
    core_pyproject = _load_pyproject(CORE_PACKAGE / "pyproject.toml")
    core_deps = _dependency_names(core_pyproject)
    forbidden = {"agent-harness-service-app", "templates", "examples"}
    leaked = sorted(core_deps & forbidden)
    if leaked:
        issues.append(f"Core package has forbidden dependencies: {', '.join(leaked)}")
    return issues


def check_template_dependency() -> list[str]:
    """确认 template 声明版本依赖，workspace source 只由根项目注入。"""

    issues: list[str] = []
    template_pyproject = _load_pyproject(TEMPLATE_PACKAGE / "pyproject.toml")
    template_deps = _dependency_names(template_pyproject)
    template_sources = _workspace_source_names(template_pyproject)
    root_sources = _workspace_source_names(_load_pyproject(ROOT / "pyproject.toml"))
    if "agent-harness" not in template_deps:
        issues.append("Service-app template must depend on agent-harness.")
    if "agent-harness" in template_sources:
        issues.append("Service-app template must not retain member-only workspace sources.")
    if "agent-harness" not in root_sources:
        issues.append("Workspace root must resolve agent-harness from the workspace.")
    return issues


def check_python_imports() -> list[str]:
    """检查源码 import 方向和 provider SDK 隔离。

    banned vendor list 的来源是公共 boundary contract；脚本只负责把它应用到
    packages、templates、examples、scripts 和 tests 的当前源码表面。
    """

    issues: list[str] = []
    for path in _python_files():
        imports = _top_level_imports(path)
        if path.is_relative_to(CORE_PACKAGE) and ({"templates", "examples"} & imports):
            issues.append(f"{path.relative_to(ROOT)} imports templates/examples from core package.")
        banned = sorted(BANNED_VENDOR_IMPORTS & imports)
        if banned and not _is_future_adapter_path(path):
            issues.append(
                f"{path.relative_to(ROOT)} imports vendor SDK outside adapters: {', '.join(banned)}"
            )
    return issues


def check_sqlalchemy_session_boundaries() -> list[str]:
    """禁止业务入口直接持有 SQLAlchemy session。

    storage adapter / migration 可以使用 ORM session；template app、示例 agent 和
    examples 必须走 repository / UnitOfWork seam。
    """

    issues: list[str] = []
    business_roots = [
        TEMPLATE_PACKAGE / "app",
        TEMPLATE_PACKAGE / "agents",
        ROOT / "examples",
    ]
    for root in business_roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            leaked = sorted(_sqlalchemy_session_imports(path))
            if leaked:
                issues.append(
                    f"{path.relative_to(ROOT)} imports SQLAlchemy session outside storage seam: "
                    f"{', '.join(leaked)}"
                )
    return issues


def main() -> int:
    """汇总依赖、导入和 session 边界问题，并返回适合 CI 门禁的退出码。"""

    issues = [
        *check_core_dependencies(),
        *check_template_dependency(),
        *check_python_imports(),
        *check_sqlalchemy_session_boundaries(),
    ]
    if issues:
        for issue in issues:
            print(f"import-boundary: {issue}", file=sys.stderr)
        return 1
    print("import-boundary: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
