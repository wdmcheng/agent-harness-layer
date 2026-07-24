"""Service-app 模板目录、CLI、文档和依赖方向合同测试。"""

from __future__ import annotations

import ast
import inspect
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

import agent_harness.cli as core_cli_module
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
    """列出模板扫描范围内的 Python 文件，并排除解释器生成的缓存目录。"""

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
        "docs/README.zh-CN.md",
        "docs/ai-agent-guide.md",
        "docs/ai-agent-guide.zh-CN.md",
        "docs/examples.md",
        "docs/examples.zh-CN.md",
        "scripts/bootstrap.py",
        "scripts/smoke_service.py",
        "docker-compose.yml",
        ".gitignore",
        ".env.example",
        "Makefile",
        "README.md",
        "README.zh-CN.md",
        "pyproject.toml",
    )

    missing = [relative for relative in required if not (TEMPLATE / relative).exists()]
    assert missing == []


def test_copied_template_ignores_local_runtime_and_secret_state(tmp_path: Path) -> None:
    """复制为独立项目后仍应阻止密钥、虚拟环境和运行证据被误提交。"""

    copied = tmp_path / "service-app"
    shutil.copytree(TEMPLATE, copied)
    subprocess.run(["git", "init", "-q"], cwd=copied, check=True)
    ignored_paths = {
        ".env",
        ".env.local",
        ".venv/state",
        ".agent-harness/state",
        "__pycache__/module.pyc",
        ".pytest_cache/state",
        ".ruff_cache/state",
        "dist/service_app.whl",
        "build/state",
    }
    result = subprocess.run(
        ["git", "-c", "core.excludesFile=/dev/null", "check-ignore", "--stdin"],
        cwd=copied,
        input="\n".join(sorted(ignored_paths)),
        text=True,
        capture_output=True,
        check=False,
    )
    ignored = set(result.stdout.splitlines())

    assert ignored == ignored_paths
    assert (
        subprocess.run(
            [
                "git",
                "-c",
                "core.excludesFile=/dev/null",
                "check-ignore",
                "-q",
                ".env.example",
            ],
            cwd=copied,
            check=False,
        ).returncode
        == 1
    )


def test_template_typer_cli_exposes_only_app_specific_serve(monkeypatch: Any) -> None:
    """模板 CLI 只装配 uvicorn，不复制核心 agents/run/eval/policy 业务命令。"""

    from app.cli import main as cli_module

    calls: dict[str, Any] = {}
    application = object()

    def fake_create_app(**kwargs: Any) -> object:
        """记录 CLI 传给 app 工厂的参数，避免测试实际启动服务组件。"""

        calls["create_app"] = kwargs
        return application

    def fake_run(target: object, **kwargs: Any) -> None:
        """记录 uvicorn 目标与网络参数，验证模板 CLI 只负责服务启动编排。"""

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
    assert "uvicorn>=0.50.2,<0.51" in pyproject["project"]["dependencies"]
    assert "agent-harness" not in pyproject.get("tool", {}).get("uv", {}).get("sources", {})
    assert pyproject["tool"]["pyright"] == {"venvPath": ".", "venv": ".venv"}
    assert {"pytest>=9.1.1,<10", "ruff>=0.15.20,<0.16", "pyright>=1.1.411,<2"} <= set(
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
    """中英文 README 都必须能让应用开发者启动，并让模板维护者守住边界。"""

    english = (TEMPLATE / "README.md").read_text(encoding="utf-8")
    chinese = (TEMPLATE / "README.zh-CN.md").read_text(encoding="utf-8")
    docs = (TEMPLATE / "docs" / "README.md").read_text(encoding="utf-8")
    docs_zh = (TEMPLATE / "docs" / "README.zh-CN.md").read_text(encoding="utf-8")
    env_example = (TEMPLATE / ".env.example").read_text(encoding="utf-8")

    for marker in (
        "make dev",
        "make bootstrap",
        "AGENT_HARNESS_SOURCE",
        "agent-harness-service serve",
        "agent-harness run examples.basic",
        "/docs",
        "/redoc",
        "AGENT_HARNESS_BUDGET__FINGERPRINT_KEY",
        "app/migrate.py",
        "AGENT_HARNESS_STORAGE__DSN",
        "scaffold agent",
    ):
        assert marker in english
        assert marker in chinese

    for marker in (
        "[简体中文](README.zh-CN.md)",
        "## First use: local profile",
        "## Ask an AI / Agent to work on the project",
        "## HTTP API",
        "## Python composition API",
        "## Ergonomic layers and “syntax sugar”",
        "## Project structure",
        "## Module design",
        "## Map your Agent to five layers and two wings",
        "## Development and testing",
        "## Contributing",
        "not a production deployment",
        "removes its containers, network, volume, temporary credentials",
    ):
        assert marker in english

    for marker in (
        "[English](README.md)",
        "## 首次使用：local profile",
        "## 让 AI / Agent 操作项目",
        "## HTTP API",
        "## Python 组合 API",
        "## 便捷封装和“语法糖”",
        "## 目录结构",
        "## 模块设计思路",
        "## 把 Agent 对应到五层两翼",
        "## 开发和测试",
        "## 贡献指南",
        "原子 Agent 生成器",
        "没有 `--force`",
        "人工审核",
        "都不是生产部署",
        "删除本轮 container、network、volume、临时 credential",
    ):
        assert marker in chinese

    for readme in (english, chinese):
        assert "eval-cases/approved" in readme
        assert "app/*" in readme and "agent_harness" in readme
        assert "agent-harness = { workspace = true }" not in readme
        assert "后续文档交付" not in readme
    assert "`tenant_id`, `agent_id`, and `run_id`" in english
    assert "`tenant_id`、`agent_id`、`run_id`" in chinese
    assert "Project-root discovery lets core CLI commands" not in english
    assert "核心 CLI 在复制后的 service-app 中自动定位" not in chinese
    assert "docs/ai-agent-guide.md" in english
    assert "docs/ai-agent-guide.zh-CN.md" in chinese
    assert "../../docs/extension-guide.md" in english
    assert "../../docs/extension-guide.zh-CN.md" in chinese
    assert "../../../docs/adapter-contracts.md" in docs
    assert "../../../docs/adapter-contracts.zh-CN.md" in docs_zh
    assert "AGENT_HARNESS_BUDGET__FINGERPRINT_KEY=" in env_example
    assert "同一状态库生命周期内保持稳定" in env_example
    assert "后续文档交付" not in docs
    assert "后续文档交付" not in docs_zh


def test_root_readme_preserves_product_overview_and_delegation_boundary() -> None:
    """保护双语根入口的产品定位与委派门禁，避免关键约束下沉后导致入口失真。"""

    english = (ROOT / "README.md").read_text(encoding="utf-8")
    chinese = (ROOT / "README.zh-CN.md").read_text(encoding="utf-8")

    for marker in (
        "## What it does",
        "## First use",
        "## Hand project work to an AI / Agent",
        "## Python API",
        "## Ergonomic layers and “syntax sugar”",
        "## Project structure",
        "## Module design",
        "## Build an Agent with five layers and two wings",
        "## Developer guide",
        "## Contributing",
        "[简体中文](README.zh-CN.md)",
    ):
        assert marker in english

    for marker in (
        "## 这个项目能做什么",
        "## 第一次使用",
        "## 把项目任务交给 AI / Agent",
        "## Python API",
        "## 便捷封装和“语法糖”",
        "## 目录结构",
        "## 模块设计思路",
        "## 用五层两翼开发一个 Agent",
        "## 开发者指南",
        "## 贡献指南",
        "[English](README.md)",
    ):
        assert marker in chinese

    for readme in (english, chinese):
        assert "AgentRegistry" in readme
        assert "PolicyEngine" in readme
        assert "delegation" in readme
    assert "docs/building-an-agent.md" in english
    assert "docs/building-an-agent.zh-CN.md" in chinese
    assert "templates/service-app/docs/ai-agent-guide.md" in english
    assert "templates/service-app/docs/ai-agent-guide.zh-CN.md" in chinese
    assert "Read templates/service-app/docs/ai-agent-guide.md first" in english
    assert "先阅读 templates/service-app/docs/ai-agent-guide.zh-CN.md" in chinese
    assert "`tenant_id`, `agent_id`, and `run_id`" in english
    assert "`tenant_id`、`agent_id`、`run_id`" in chinese


def test_run_commands_keep_explicit_agents_dir_separate_from_scaffold_discovery() -> None:
    """run/list 仍使用源仓库默认路径，README 只能把项目根发现归给 scaffold。"""

    expected_default = Path("templates/service-app/agents")

    assert (
        inspect.signature(core_cli_module.run).parameters["agents_dir"].default == expected_default
    )
    assert (
        inspect.signature(core_cli_module.list_agents).parameters["agents_dir"].default
        == expected_default
    )

    english = (TEMPLATE / "README.md").read_text(encoding="utf-8")
    chinese = (TEMPLATE / "README.zh-CN.md").read_text(encoding="utf-8")
    assert "Project-root discovery belongs only to `scaffold agent`" in english
    assert "只有 `scaffold agent`" in chinese
    assert "`run` and `agents list` still require an explicit `--agents-dir ./agents`" in english
    assert "`run` 和 `agents list` 仍需显式传 `--agents-dir ./agents`" in chinese


def test_five_layer_two_wing_guide_maps_architecture_to_agent_work() -> None:
    """五层两翼不能只停在架构图，必须能映射到创建 Agent 的实际动作。"""

    english = (ROOT / "docs" / "building-an-agent.md").read_text(encoding="utf-8")
    chinese = (ROOT / "docs" / "building-an-agent.zh-CN.md").read_text(encoding="utf-8")
    architecture = (ROOT / "docs" / "architecture" / "README.md").read_text(encoding="utf-8")
    architecture_zh = (ROOT / "docs" / "architecture" / "README.zh-CN.md").read_text(
        encoding="utf-8"
    )

    for marker in (
        "1. Access and interaction",
        "2. Orchestration and Runtime",
        "3. Engine and cognition",
        "4. Tools and capabilities",
        "5. Infrastructure and data",
        "Left wing: Eval Gate",
        "Right wing: Observability",
        "support.triage",
        "AgentRegistry.load_from_directory()",
        "AgentExecutionResult.completed(output)",
        "ToolRegistry",
        "GraphState",
        "future extension points",
    ):
        assert marker in english

    for marker in (
        "1. 接入与交互层 Access",
        "2. 编排与运行时层 Runtime",
        "3. 引擎与认知层 Engine",
        "4. 工具与能力层 Tools",
        "5. 基础设施与数据层 Infra",
        "左翼 Eval Gate",
        "右翼 Observability",
        "support.triage",
        "AgentRegistry.load_from_directory()",
        "AgentExecutionResult.completed(output)",
        "ToolRegistry",
        "GraphState",
        "目标扩展位",
    ):
        assert marker in chinese

    assert "[简体中文](building-an-agent.zh-CN.md)" in english
    assert "[English](building-an-agent.md)" in chinese
    assert "../building-an-agent.md" in architecture
    assert "conceptual tool-registration label" in architecture
    assert "../building-an-agent.zh-CN.md" in architecture_zh
    assert "概念性工具注册标签" in architecture_zh


def test_ai_agent_guide_is_opt_in_bilingual_and_actionable() -> None:
    """AI 指南必须能按需交给工具使用，不能伪装成自动生效的目录级指令。"""

    english = (TEMPLATE / "docs" / "ai-agent-guide.md").read_text(encoding="utf-8")
    chinese = (TEMPLATE / "docs" / "ai-agent-guide.zh-CN.md").read_text(encoding="utf-8")
    english_readme = (TEMPLATE / "README.md").read_text(encoding="utf-8")
    chinese_readme = (TEMPLATE / "README.zh-CN.md").read_text(encoding="utf-8")

    special_instruction_files = sorted(
        path.relative_to(TEMPLATE)
        for name in ("AGENTS.md", "AGENTS.zh-CN.md")
        for path in TEMPLATE.rglob(name)
    )
    assert special_instruction_files == []
    assert "ordinary, opt-in project guide" in english
    assert "普通、按需使用的项目指南" in chinese
    assert "[简体中文](ai-agent-guide.zh-CN.md)" in english
    assert "[English](ai-agent-guide.md)" in chinese

    for marker in (
        "AGENT_HARNESS_SOURCE",
        "make bootstrap",
        "app/migrate.py",
        "make smoke-local",
        "agent-harness scaffold agent",
        "--agents-dir ./agents",
        "make eval",
        "make smoke-service",
        "tenant_id",
        "agent_id",
        "run_id",
    ):
        assert marker in english
        assert marker in chinese

    for marker in (
        "Do not assume those files exist in a standalone copy",
        "Do not silently install a public same-name package",
        "Reuse the same key for the lifetime",
        "Project-root discovery belongs only to `scaffold agent`",
        "does not import vendor SDKs",
        "automation never self-approves",
        "Local/fake evidence does not prove",
        "require explicit user authorization",
        "## Required handoff",
    ):
        assert marker in english

    for marker in (
        "独立复制项目不得假设它们仍在",
        "不得静默安装公网上的同名包",
        "整个生命周期必须复用同一 key",
        "项目根自动发现只有 `scaffold agent` 使用",
        "不得直接 import vendor SDK",
        "自动化不得自行批准",
        "local/fake 证据不能证明",
        "都需要用户单独明确授权",
        "## 必须返回的交付说明",
    ):
        assert marker in chinese

    assert "## Copyable prompts" in english
    assert "## 可复制提示词" in chinese
    assert "docs/ai-agent-guide.md" in english_readme
    assert "docs/ai-agent-guide.zh-CN.md" in chinese_readme


def test_service_boundary_adr_links_back_to_maintainer_navigation() -> None:
    """确保服务边界 ADR 能返回维护者入口，并能继续访问两个直接相关的架构决策。"""

    adr_0001 = (ROOT / "docs" / "adr" / "0001-p0-service-boundaries.md").read_text(encoding="utf-8")

    for marker in ("../../README.md", "../architecture/README.md", "0002-", "0003-"):
        assert marker in adr_0001


def test_tool_guide_distinguishes_public_exports_from_internal_executor() -> None:
    """防止扩展指南把审批执行内部实现误写为公开导出，迫使调用方依赖不稳定模块。"""

    extension = (ROOT / "docs" / "extension-guide.md").read_text(encoding="utf-8")
    extension_zh = (ROOT / "docs" / "extension-guide.zh-CN.md").read_text(encoding="utf-8")

    assert "ApprovedToolExecutor` is an internal registry approval executor" in extension
    assert "ApprovedToolExecutor` 是 registry 内部" in extension_zh
    assert "ApprovedToolExecutor`、" not in extension_zh


def test_adapter_contract_records_complete_sqlalchemy_ownership_boundary() -> None:
    """锁定 ORM 的受控所有权，避免维护者在 repository 与 UoW 之外直接操作 session。"""

    adapters = (ROOT / "docs" / "adapter-contracts.md").read_text(encoding="utf-8")
    adapters_zh = (ROOT / "docs" / "adapter-contracts.zh-CN.md").read_text(encoding="utf-8")

    assert "models, repositories, and migrations under `storage`" in adapters
    assert "APIs, workers, and services compose repositories/UoW only" in adapters
    assert "model、repository、migration" in adapters_zh
    assert "API、worker 和 service 只能组合 repository/UoW" in adapters_zh
    assert "唯一允许接触相应 SDK、driver 或 ORM" not in adapters_zh


def test_redis_runtime_adr_records_triggered_security_review_before_release() -> None:
    """让 Redis 复审决策绑定真实 Compose 入口，防止无效变量掩盖生产前的安全升级。"""

    redis_adr = (ROOT / "docs" / "adr" / "0003-redis-runtime-license-policy.md").read_text(
        encoding="utf-8"
    )
    redis_adr_zh = (ROOT / "docs" / "adr" / "0003-redis-runtime-license-policy.zh-CN.md").read_text(
        encoding="utf-8"
    )
    compose = (TEMPLATE / "docker-compose.yml").read_text(encoding="utf-8")

    assert "the security-review trigger fired" in redis_adr
    assert "Production use/release" in redis_adr
    assert "returning to the approved 7.2 license line" in redis_adr
    assert "SERVICE_APP_REDIS_IMAGE" in redis_adr
    assert "SERVICE_APP_REDIS_VERSION" not in redis_adr
    assert "安全复审条件已经触发" in redis_adr_zh
    assert "生产使用或" in redis_adr_zh
    assert "重新选择补丁版本" in redis_adr_zh
    assert (
        "${SERVICE_APP_REDIS_IMAGE:-redis:7.2.14@sha256:f0707c78ea880b293ccdeb410c9c0a8ccae93fe7128799b751333a698b0a39a7}"
        in compose
    )


def test_cli_inventory_keeps_core_and_template_ownership_separate() -> None:
    """公开管理命令含 scaffold 归核心 CLI；模板仍只拥有 serve。"""

    runner = CliRunner()
    core_help = runner.invoke(core_cli, ["--help"])

    assert core_help.exit_code == 0
    for command in ("doctor", "agents", "run", "approvals", "eval", "policy", "scaffold"):
        assert command in core_help.output
