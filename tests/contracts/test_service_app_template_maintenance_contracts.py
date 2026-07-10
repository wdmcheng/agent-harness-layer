"""Service-app 模板目录、CLI、文档和依赖方向合同测试。"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from agent_harness.cli import app as core_cli

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "templates" / "service-app"


def _import_roots(path: Path) -> set[str]:
    """从 Python 源码提取 import 根名，静态验证依赖方向而不执行模块。"""

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".", 1)[0])
    return roots


def _python_files(root: Path) -> list[Path]:
    return [path for path in root.rglob("*.py") if "__pycache__" not in path.parts]


def test_template_layout_contains_committable_maintenance_content() -> None:
    """关键空目录必须有可复制内容，不能只依赖运行时顺手创建。"""

    required = (
        "app/api",
        "app/cli",
        "app/workers",
        "agents/examples",
        "configs/profiles/local.yaml",
        "configs/profiles/service.yaml",
        "eval-cases/drafts/README.md",
        "eval-cases/approved/README.md",
        "tests/test_app_surface.py",
        "tests/test_bootstrap.py",
        "docs/README.md",
        "scripts/bootstrap.py",
        "scripts/smoke_service.py",
        "docker-compose.yml",
        ".env.example",
        "Makefile",
        "README.md",
        "pyproject.toml",
    )

    missing = [relative for relative in required if not (TEMPLATE / relative).exists()]
    assert missing == []


def test_template_typer_cli_exposes_only_app_specific_serve(monkeypatch: Any) -> None:
    """模板 CLI 只装配 uvicorn，不复制核心 agents/run/eval/policy 业务命令。"""

    from app.cli import main as cli_module

    calls: dict[str, Any] = {}
    application = object()

    def fake_create_app(**kwargs: Any) -> object:
        calls["create_app"] = kwargs
        return application

    def fake_run(target: object, **kwargs: Any) -> None:
        calls["uvicorn_target"] = target
        calls["uvicorn"] = kwargs

    monkeypatch.setattr(cli_module, "create_app", fake_create_app)
    monkeypatch.setattr(cli_module.uvicorn, "run", fake_run)
    runner = CliRunner()

    help_result = runner.invoke(cli_module.cli, ["--help"])
    serve_result = runner.invoke(
        cli_module.cli,
        [
            "serve",
            "--profile",
            "service",
            "--profiles-dir",
            str(TEMPLATE / "configs" / "profiles"),
            "--storage-dsn",
            "sqlite+aiosqlite:///:memory:",
            "--events-path",
            str(TEMPLATE / ".agent-harness" / "events.jsonl"),
            "--host",
            "0.0.0.0",
            "--port",
            "9123",
        ],
    )

    assert help_result.exit_code == 0
    assert "serve" in help_result.output
    for duplicated in ("agents", "run", "approvals", "eval", "policy", "scaffold"):
        assert duplicated not in help_result.output
    assert serve_result.exit_code == 0, serve_result.output
    assert calls["create_app"] == {
        "profile": "service",
        "profiles_dir": TEMPLATE / "configs" / "profiles",
        "storage_dsn": "sqlite+aiosqlite:///:memory:",
        "events_path": TEMPLATE / ".agent-harness" / "events.jsonl",
    }
    assert calls["uvicorn_target"] is application
    assert calls["uvicorn"] == {"host": "0.0.0.0", "port": 9123}


def test_template_pyproject_and_makefile_publish_dev_entrypoints() -> None:
    """安装元数据和 Makefile 都必须把 app CLI、dev、测试和核心 CLI 委托暴露出来。"""

    pyproject = tomllib.loads((TEMPLATE / "pyproject.toml").read_text(encoding="utf-8"))
    makefile = (TEMPLATE / "Makefile").read_text(encoding="utf-8")

    assert pyproject["project"]["scripts"]["agent-harness-service"] == "app.cli.main:main"
    assert "uvicorn==0.50.2" in pyproject["project"]["dependencies"]
    assert "agent-harness" not in pyproject.get("tool", {}).get("uv", {}).get("sources", {})
    assert {"pytest==9.1.1", "ruff==0.15.20", "pyright==1.1.411"} <= set(
        pyproject["dependency-groups"]["dev"]
    )
    for target in ("bootstrap:", "dev:", "cli:", "test:", "contract:", "quality:"):
        assert target in makefile
    assert "agent-harness-service serve" in makefile
    assert "agent-harness $(ARGS)" in makefile
    assert "cd ../.." not in makefile
    assert "scripts/smoke_service.py" in makefile
    assert "AGENT_HARNESS_SOURCE" in makefile


def test_template_and_eval_runner_keep_vendor_and_orm_boundary() -> None:
    """模板入口和 eval runner 只能经核心 seam 访问 vendor/storage。"""

    scan_roots = (
        TEMPLATE / "app",
        TEMPLATE / "agents",
        ROOT / "packages" / "agent-harness" / "src" / "agent_harness" / "evals",
    )
    forbidden = {"pydantic_ai", "dbos", "logfire", "phoenix", "langfuse", "sqlalchemy"}
    violations: list[str] = []
    for scan_root in scan_roots:
        for path in _python_files(scan_root):
            imported = _import_roots(path) & forbidden
            if imported:
                violations.append(f"{path.relative_to(ROOT)}: {', '.join(sorted(imported))}")

    assert violations == []


def test_core_package_does_not_depend_on_template_modules() -> None:
    """反向扫描核心包，禁止 import `app` 或模板 `agents` 模块。"""

    core = ROOT / "packages" / "agent-harness" / "src" / "agent_harness"
    violations: list[str] = []
    for path in _python_files(core):
        imported = _import_roots(path) & {"app", "agents"}
        if imported:
            violations.append(f"{path.relative_to(ROOT)}: {', '.join(sorted(imported))}")

    assert violations == []


def test_readme_serves_both_audiences_and_records_delivery_boundaries() -> None:
    """README 必须能让应用开发者启动，也能让模板维护者守住边界。"""

    readme = (TEMPLATE / "README.md").read_text(encoding="utf-8")
    docs = (TEMPLATE / "docs" / "README.md").read_text(encoding="utf-8")

    for marker in (
        "## Quick Start",
        "## Project Structure",
        "## For Agent App Developers",
        "## For Scaffold Maintainers",
        "make dev",
        "make bootstrap",
        "AGENT_HARNESS_SOURCE",
        "agent-harness-service serve",
        "agent-harness run examples.basic",
        "/docs",
        "/redoc",
        "后续文档交付",
        "scaffold agent",
    ):
        assert marker in readme
    assert "原子生成" in readme
    assert "无 `--force`" in readme
    assert "eval-cases/approved" in readme
    assert "审核" in readme
    assert "app/*" in readme and "agent_harness" in readme
    assert "agent-harness = { workspace = true }" not in readme
    assert "后续文档交付" in docs


def test_p0_cli_inventory_keeps_core_and_template_ownership_separate() -> None:
    """P0 管理命令含 scaffold 归核心 CLI；模板仍只拥有 serve。"""

    runner = CliRunner()
    core_help = runner.invoke(core_cli, ["--help"])

    assert core_help.exit_code == 0
    for command in ("doctor", "agents", "run", "approvals", "eval", "policy", "scaffold"):
        assert command in core_help.output
