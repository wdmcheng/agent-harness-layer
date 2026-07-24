"""Workspace packaging 和 template shell 的公开契约测试。

这些测试锁 public seam：开发者实际会碰到的 workspace metadata、包 import、
template wheel boundary 和 local profile。它们不证明预留目录里的后续能力已实现。
"""

from __future__ import annotations

import importlib
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from packaging.requirements import Requirement

from agent_harness.config import load_settings

ROOT = Path(__file__).resolve().parents[2]


def load_pyproject(path: Path) -> dict[str, object]:
    """以二进制模式读取 TOML，并返回供合同断言使用的顶层对象。"""

    with path.open("rb") as file:
        return cast(dict[str, object], tomllib.load(file))


def as_mapping(value: object) -> Mapping[str, object]:
    """将已断言为字典的 TOML 节点收窄为只读映射，避免测试隐式接受错误形状。"""

    assert isinstance(value, dict)
    return cast(Mapping[str, object], value)


def test_agent_harness_package_exposes_version() -> None:
    """验证核心包提供稳定版本号，供构建、doctor 与模板依赖共同引用。"""

    # 版本是 build、doctor 和 template dependency 共用的最小 package seam。
    module = importlib.import_module("agent_harness")

    assert isinstance(module.__version__, str)
    assert module.__version__ == "0.1.0"


def test_workspace_members_are_declared() -> None:
    """验证根 workspace 声明核心包与服务模板，保证 monorepo 解析边界完整。"""

    # workspace members 是 monorepo 解析边界；少一个成员会让 path dependency 证据失真。
    pyproject = load_pyproject(ROOT / "pyproject.toml")
    tool = as_mapping(pyproject["tool"])
    uv_config = as_mapping(tool["uv"])
    workspace = as_mapping(uv_config["workspace"])

    assert workspace["members"] == [
        "packages/agent-harness",
        "templates/service-app",
    ]


def test_top_level_boundaries_exist() -> None:
    """验证顶层目录保持既定架构分区，避免可交付物与核心代码混放。"""

    # 顶层目录存在性只锁架构分区，避免后续把 scripts/templates/examples 混进 core。
    for relative_path in ["packages", "templates", "examples", "docs", "scripts"]:
        assert (ROOT / relative_path).exists()


def test_service_app_shell_layout_exists() -> None:
    """验证模板服务应用提供开发者入口目录，但不将目录存在误当作功能完成。"""

    # shell layout 是 app developer 的入口契约；目录存在不代表 runtime/API 已实现。
    service_app = ROOT / "templates" / "service-app"
    required = [
        "app/api",
        "app/cli",
        "app/workers",
        "agents/examples",
        "configs/profiles",
        "eval-cases/drafts",
        "eval-cases/approved",
        "tests",
        "docs",
        ".gitignore",
        ".env.example",
        "Makefile",
        "README.md",
        "pyproject.toml",
    ]

    for relative_path in required:
        assert (service_app / relative_path).exists()


def test_service_app_declares_core_dependency_and_source_workspace_mapping() -> None:
    """验证模板声明核心依赖，并在源 workspace 内复用根核心包。"""

    # 这里锁依赖身份和源 workspace 的解析关系，不复制发布版本合同；版本升级由
    # 专门的依赖/发布合同验证，独立复制态则由 bootstrap 与 copy-out smoke 验证。
    pyproject = load_pyproject(ROOT / "templates" / "service-app" / "pyproject.toml")
    project = as_mapping(pyproject["project"])
    tool = as_mapping(pyproject["tool"])

    raw_dependencies = project["dependencies"]
    assert isinstance(raw_dependencies, list)
    dependencies = cast(list[object], raw_dependencies)
    dependency_names: list[str] = []
    for dependency in dependencies:
        assert isinstance(dependency, str)
        dependency_names.append(Requirement(dependency).name)
    assert dependency_names.count("agent-harness") == 1
    template_uv: Mapping[str, object] = (
        as_mapping(tool["uv"]) if "uv" in tool else cast(Mapping[str, object], {})
    )
    template_sources: Mapping[str, object] = (
        as_mapping(template_uv["sources"])
        if "sources" in template_uv
        else cast(Mapping[str, object], {})
    )
    assert as_mapping(template_sources["agent-harness"]) == {"workspace": True}

    root_pyproject = load_pyproject(ROOT / "pyproject.toml")
    root_tool = as_mapping(root_pyproject["tool"])
    root_uv = as_mapping(root_tool["uv"])
    assert as_mapping(root_uv["sources"])["agent-harness"] == {"workspace": True}


def test_service_app_declares_installable_package_boundary() -> None:
    """验证模板 wheel 仅打包 app 与 agents，避免配置和文档意外成为顶层包。"""

    pyproject = load_pyproject(ROOT / "templates" / "service-app" / "pyproject.toml")
    build_system = as_mapping(pyproject["build-system"])
    tool = as_mapping(pyproject["tool"])
    hatch_config = as_mapping(tool["hatch"])
    build_config = as_mapping(hatch_config["build"])
    targets = as_mapping(build_config["targets"])
    wheel = as_mapping(targets["wheel"])

    # 这锁住模板 wheel 边界，防止 build backend 把 configs/docs 当成顶层包。
    assert build_system["build-backend"] == "hatchling.build"
    assert build_system["requires"] == ["hatchling>=1.30.1,<2"]
    assert wheel["packages"] == ["app", "agents"]


def test_local_profile_is_parseable_without_provider_keys() -> None:
    """验证离线 local profile 不要求真实 provider key，保持最小 smoke 可运行。"""

    # local profile 是离线 smoke seam；它不能被未来 provider adapter 改成需要真实 key。
    profiles_dir = ROOT / "templates" / "service-app" / "configs" / "profiles"
    settings = load_settings(profile="local", profiles_dir=profiles_dir)

    assert settings.profile == "local"
    assert settings.model.provider == "fake"
    assert settings.model.requires_api_key is False
