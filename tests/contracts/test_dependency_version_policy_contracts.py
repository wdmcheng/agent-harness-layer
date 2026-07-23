"""Python 依赖支持范围、精确 lock 与工具版本分层合同。"""

from __future__ import annotations

import hashlib
import json
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
REVIEWED_LOCK_IDENTITY_COUNT = 207
REVIEWED_LOCK_IDENTITY_SHA256 = "bb9046c25267f611007c6b74ee74c3ff8e55f885b3f92d091aed0642c5adef58"


def _load(relative: str) -> dict[str, Any]:
    """读取一份 package metadata，避免字符串扫描误把注释当成依赖。"""

    with (ROOT / relative).open("rb") as stream:
        return tomllib.load(stream)


def test_all_python_dependency_declarations_use_reviewed_compatible_ranges() -> None:
    """外部依赖表达支持窗口，同仓库自依赖精确匹配项目版本。"""

    root = _load("pyproject.toml")
    core = _load("packages/agent-harness/pyproject.toml")
    template = _load("templates/service-app/pyproject.toml")

    assert set(root["project"]["dependencies"]) == {"agent-harness==0.1.0"}
    assert set(root["dependency-groups"]["dev"]) == {
        "coverage>=7.15.0,<8",
        "fastapi>=0.139.0,<0.140",
        "pre-commit>=4.6.0,<5",
        "pyright>=1.1.411,<2",
        "pytest>=9.1.1,<10",
        "pytest-asyncio>=1.4.0,<2",
        "ruff>=0.15.20,<0.16",
    }
    assert set(root["dependency-groups"]["license"]) == {"licensecheck>=2026.0.8,<2027"}
    assert set(root["dependency-groups"]["release"]) == {
        "hatchling>=1.30.1,<2",
        "python-semantic-release>=10.6.1,<11",
    }

    assert set(core["project"]["dependencies"]) == {
        "aiosqlite>=0.22.1,<0.23",
        "alembic>=1.18.5,<2",
        "asyncpg>=0.31.0,<0.32",
        "dbos>=2.26.0,<3",
        "greenlet>=3.3.0,<4",
        "httpx>=0.28.1,<0.29",
        "mcp>=1.28.1,<2",
        "pydantic>=2.13.4,<3",
        "pydantic-ai>=2.5.0,<3",
        "PyYAML>=6.0.3,<7",
        "redis>=8.0.1,<9",
        "SQLAlchemy>=2.0.51,<3",
        "typer>=0.26.8,<0.27",
    }
    assert set(core["project"]["optional-dependencies"]["observability"]) == {
        "arize-phoenix>=17.21.0,<18",
        "langfuse>=4.13.2,<5",
        "logfire[httpx]>=4.37.0,<5",
        "opentelemetry-api>=1.42.1,<1.43",
        "opentelemetry-exporter-otlp-proto-http>=1.42.1,<1.43",
        "opentelemetry-sdk>=1.42.1,<1.43",
    }
    assert core["build-system"]["requires"] == ["hatchling>=1.30.1,<2"]

    assert set(template["project"]["dependencies"]) == {
        "agent-harness==0.1.0",
        "fastapi>=0.139.0,<0.140",
        "typer>=0.26.8,<0.27",
        "uvicorn>=0.50.2,<0.51",
    }
    assert set(template["dependency-groups"]["dev"]) == {
        "pyright>=1.1.411,<2",
        "pytest>=9.1.1,<10",
        "ruff>=0.15.20,<0.16",
    }
    assert template["build-system"]["requires"] == ["hatchling>=1.30.1,<2"]

    external_requirements = [
        *(item for group in root["dependency-groups"].values() for item in group),
        *core["project"]["dependencies"],
        *core["project"]["optional-dependencies"]["observability"],
        *core["build-system"]["requires"],
        *(
            item
            for item in template["project"]["dependencies"]
            if not item.startswith("agent-harness==")
        ),
        *(item for group in template["dependency-groups"].values() for item in group),
        *template["build-system"]["requires"],
    ]
    assert all("==" not in requirement for requirement in external_requirements)


def test_local_uv_range_and_conflicting_groups_keep_release_baseline_exact() -> None:
    """本地 patch 兼容不能放松 CI/release 版本或合并互斥工具环境。"""

    root = _load("pyproject.toml")
    assert root["tool"]["uv"]["required-version"] == ">=0.11.19,<0.12"
    assert root["tool"]["uv"]["conflicts"] == [[{"group": "release"}, {"group": "license"}]]

    plan = (ROOT / "DEV-PLAN.md").read_text(encoding="utf-8")
    policy = (ROOT / "openspec/specs/dependency-version-policy/spec.md").read_text(encoding="utf-8")
    assert "uv sync --frozen --all-groups" not in plan + policy
    assert "--group release --no-group license" in plan + policy
    assert "--group license --no-group release" in plan + policy

    release_support = (ROOT / "scripts/release_contract_support.py").read_text(encoding="utf-8")
    assert 'UV_VERSION = "0.11.29"' in release_support
    for relative in (".github/workflows/ci.yml", ".github/workflows/release.yml"):
        workflow = (ROOT / relative).read_text(encoding="utf-8")
        assert 'version: "0.11.29"' in workflow
    assert "uv:0.11.29-python3.12-trixie-slim@sha256:" in (ROOT / ".gitlab-ci.yml").read_text(
        encoding="utf-8"
    )


def test_lock_package_identities_match_reviewed_baseline() -> None:
    """依赖升级必须显式更新受审 identity 基线，不能混入普通 metadata 变更。"""

    lock = _load("uv.lock")
    rows = sorted(
        (
            package["name"],
            package["version"],
            json.dumps(package.get("source", {}), sort_keys=True, separators=(",", ":")),
        )
        for package in lock["package"]
    )
    encoded = "\n".join("\t".join(row) for row in rows).encode()

    assert len(rows) == REVIEWED_LOCK_IDENTITY_COUNT
    assert hashlib.sha256(encoded).hexdigest() == REVIEWED_LOCK_IDENTITY_SHA256


def test_bilingual_docs_explain_range_lock_and_exact_release_baseline() -> None:
    """根、模板与发布文档都必须解释声明、lock、自依赖和 uv 的不同边界。"""

    for relative in (
        "README.md",
        "README.zh-CN.md",
        "templates/service-app/README.md",
        "templates/service-app/README.zh-CN.md",
        "docs/release-process.md",
        "docs/release-process.zh-CN.md",
    ):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert ">=0.11.19,<0.12" in text
        assert "0.11.29" in text
        assert "uv.lock" in text

    for relative in ("README.md", "README.zh-CN.md"):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "agent-harness==0.1.0" in text
        assert "uv lock --upgrade" in text
