from __future__ import annotations

import importlib
import json
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import cast

ROOT = Path(__file__).resolve().parents[2]


def load_pyproject(path: Path) -> dict[str, object]:
    with path.open("rb") as file:
        return cast(dict[str, object], tomllib.load(file))


def as_mapping(value: object) -> Mapping[str, object]:
    assert isinstance(value, dict)
    return cast(Mapping[str, object], value)


def test_agent_harness_package_exposes_version() -> None:
    module = importlib.import_module("agent_harness")

    assert isinstance(module.__version__, str)
    assert module.__version__ == "0.1.0"


def test_workspace_members_are_declared() -> None:
    pyproject = load_pyproject(ROOT / "pyproject.toml")
    tool = as_mapping(pyproject["tool"])
    uv_config = as_mapping(tool["uv"])
    workspace = as_mapping(uv_config["workspace"])

    assert workspace["members"] == [
        "packages/agent-harness",
        "templates/service-app",
    ]


def test_top_level_boundaries_exist() -> None:
    for relative_path in ["packages", "templates", "examples", "docs", "scripts"]:
        assert (ROOT / relative_path).exists()


def test_service_app_shell_layout_exists() -> None:
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
    pyproject = load_pyproject(ROOT / "templates" / "service-app" / "pyproject.toml")
    project = as_mapping(pyproject["project"])
    tool = as_mapping(pyproject["tool"])
    uv_config = as_mapping(tool["uv"])
    sources = as_mapping(uv_config["sources"])

    dependencies = project["dependencies"]
    assert isinstance(dependencies, list)
    assert "agent-harness==0.1.0" in dependencies
    assert sources["agent-harness"] == {"workspace": True}


def test_local_profile_is_parseable_without_provider_keys() -> None:
    profile_path = ROOT / "templates" / "service-app" / "configs" / "profiles" / "local.yaml"
    profile_data: object = json.loads(profile_path.read_text(encoding="utf-8"))
    assert isinstance(profile_data, dict)
    profile = cast(Mapping[str, object], profile_data)
    model = as_mapping(profile["model"])

    assert profile["profile"] == "local"
    assert model["provider"] == "fake"
    assert model["requires_api_key"] is False
