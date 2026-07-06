"""Check Phase 1 package and vendor import boundaries."""

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
sys.path.insert(0, str(CORE_PACKAGE / "src"))

from agent_harness.contracts.boundaries import (  # noqa: E402
    BANNED_VENDOR_IMPORTS,
    is_vendor_import_allowed,
)


def _load_pyproject(path: Path) -> dict[str, object]:
    with path.open("rb") as file:
        return cast(dict[str, object], tomllib.load(file))


def _as_mapping(value: object) -> Mapping[str, object] | None:
    if not isinstance(value, dict):
        return None
    return cast(Mapping[str, object], value)


def _dependency_names(pyproject: Mapping[str, object]) -> set[str]:
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
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".", 1)[0])
    return imports


def _is_future_adapter_path(path: Path) -> bool:
    return is_vendor_import_allowed(path.relative_to(ROOT))


def check_core_dependencies() -> list[str]:
    issues: list[str] = []
    core_pyproject = _load_pyproject(CORE_PACKAGE / "pyproject.toml")
    core_deps = _dependency_names(core_pyproject)
    forbidden = {"agent-harness-service-app", "templates", "examples"}
    leaked = sorted(core_deps & forbidden)
    if leaked:
        issues.append(f"Core package has forbidden dependencies: {', '.join(leaked)}")
    return issues


def check_template_dependency() -> list[str]:
    issues: list[str] = []
    template_pyproject = _load_pyproject(TEMPLATE_PACKAGE / "pyproject.toml")
    template_deps = _dependency_names(template_pyproject)
    workspace_sources = _workspace_source_names(template_pyproject)
    if "agent-harness" not in template_deps:
        issues.append("Service-app template must depend on agent-harness.")
    if "agent-harness" not in workspace_sources:
        issues.append("Service-app template must resolve agent-harness from the workspace.")
    return issues


def check_python_imports() -> list[str]:
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


def main() -> int:
    issues = [
        *check_core_dependencies(),
        *check_template_dependency(),
        *check_python_imports(),
    ]
    if issues:
        for issue in issues:
            print(f"import-boundary: {issue}", file=sys.stderr)
        return 1
    print("import-boundary: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
