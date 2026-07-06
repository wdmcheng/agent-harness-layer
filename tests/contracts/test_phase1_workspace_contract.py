"""Phase 1 workspace、packaging 和 template shell 的公开契约测试。

这些测试锁 public seam：开发者实际会碰到的 workspace metadata、包 import、
template wheel boundary 和 local profile。它们不证明预留目录里的后续 Phase 能力已实现。
"""

from __future__ import annotations

import importlib
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from agent_harness.config import load_settings

ROOT = Path(__file__).resolve().parents[2]


def load_pyproject(path: Path) -> dict[str, object]:
    with path.open("rb") as file:
        return cast(dict[str, object], tomllib.load(file))


def as_mapping(value: object) -> Mapping[str, object]:
    assert isinstance(value, dict)
    return cast(Mapping[str, object], value)


def test_agent_harness_package_exposes_version() -> None:
    # 版本是 build、doctor 和 template dependency 共用的最小 package seam。
    module = importlib.import_module("agent_harness")

    assert isinstance(module.__version__, str)
    assert module.__version__ == "0.1.0"


def test_workspace_members_are_declared() -> None:
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
    # 顶层目录存在性只锁架构分区，避免后续把 scripts/templates/examples 混进 core。
    for relative_path in ["packages", "templates", "examples", "docs", "scripts"]:
        assert (ROOT / relative_path).exists()


def test_service_app_shell_layout_exists() -> None:
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
        ".env.example",
        "Makefile",
        "README.md",
        "pyproject.toml",
    ]

    for relative_path in required:
        assert (service_app / relative_path).exists()


def test_service_app_depends_on_workspace_core_package() -> None:
    # template 必须通过 package dependency 消费 core，不能靠相对源码 import 混过 smoke。
    pyproject = load_pyproject(ROOT / "templates" / "service-app" / "pyproject.toml")
    project = as_mapping(pyproject["project"])
    tool = as_mapping(pyproject["tool"])
    uv_config = as_mapping(tool["uv"])
    sources = as_mapping(uv_config["sources"])

    dependencies = project["dependencies"]
    assert isinstance(dependencies, list)
    assert "agent-harness==0.1.0" in dependencies
    assert sources["agent-harness"] == {"workspace": True}


def test_service_app_declares_installable_package_boundary() -> None:
    pyproject = load_pyproject(ROOT / "templates" / "service-app" / "pyproject.toml")
    build_system = as_mapping(pyproject["build-system"])
    tool = as_mapping(pyproject["tool"])
    hatch_config = as_mapping(tool["hatch"])
    build_config = as_mapping(hatch_config["build"])
    targets = as_mapping(build_config["targets"])
    wheel = as_mapping(targets["wheel"])

    # 这锁住模板 wheel 边界，防止 build backend 把 configs/docs 当成顶层包。
    assert build_system["build-backend"] == "hatchling.build"
    assert build_system["requires"] == ["hatchling==1.30.1"]
    assert wheel["packages"] == ["app", "agents"]


def test_local_profile_is_parseable_without_provider_keys() -> None:
    # local profile 是离线 smoke seam；它不能被未来 provider adapter 改成需要真实 key。
    profiles_dir = ROOT / "templates" / "service-app" / "configs" / "profiles"
    settings = load_settings(profile="local", profiles_dir=profiles_dir)

    assert settings.profile == "local"
    assert settings.model.provider == "fake"
    assert settings.model.requires_api_key is False
