"""Vendor boundary 和 doctor CLI seam 的契约测试。

vendor allowlist 的事实来源在 `agent_harness.contracts.boundaries`；这些测试只确认
公开声明会被质量门禁和 CLI smoke 共同使用，不在测试里复制第二份规则。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from scripts.import_boundary_check import (
    check_python_imports,
    check_sqlalchemy_session_boundaries,
)

from agent_harness.contracts.boundaries import (
    BANNED_VENDOR_IMPORTS,
    is_vendor_import_allowed,
)

ROOT = Path(__file__).resolve().parents[2]
PROFILES = ROOT / "templates" / "service-app" / "configs" / "profiles"


def test_boundary_contract_lists_banned_vendors_and_adapter_allowlist() -> None:
    """验证 vendor 禁止集和按目录授权的 adapter 例外来自同一公开边界定义。"""

    # allowlist 按目录职责判断，不按单个文件白名单判断，方便后续 adapter 扩展。
    assert {"pydantic_ai", "openai", "dbos", "logfire", "phoenix", "langfuse"} <= (
        BANNED_VENDOR_IMPORTS
    )
    assert is_vendor_import_allowed(
        Path("packages/agent-harness/src/agent_harness/adapters/model.py")
    )
    assert not is_vendor_import_allowed(Path("templates/service-app/app/api/run.py"))
    assert not is_vendor_import_allowed(
        Path("templates/service-app/agents/evil/adapters/openai.py")
    )
    assert not is_vendor_import_allowed(Path("templates/service-app/app/integrations/openai.py"))


def test_example_agents_have_no_direct_vendor_sdk_imports() -> None:
    """对示例 Agent 源码实际执行 vendor import 门禁，而非只检查禁止集合。"""

    example_files = sorted((ROOT / "templates" / "service-app" / "agents").rglob("*.py"))
    assert example_files, "示例 Agent 源码目录不得为空"
    issues = check_python_imports()

    assert not [
        issue
        for issue in issues
        if issue.startswith("templates/service-app/agents/") or issue.startswith("examples/")
    ]


def test_business_agents_have_no_vendor_or_orm_session_imports() -> None:
    """业务 Agent 同时经过 vendor SDK 与 ORM session 的生产静态扫描。"""

    business_files = sorted((ROOT / "templates" / "service-app" / "agents").rglob("*.py"))
    assert business_files, "业务 Agent 源码目录不得为空"

    assert check_python_imports() == []
    assert check_sqlalchemy_session_boundaries() == []


def test_doctor_cli_reports_local_profile_without_provider_keys() -> None:
    """验证无副作用的 doctor 可在 local profile 下运行，不要求真实 provider 密钥。"""

    # doctor 是无副作用诊断：local profile 成功不应依赖真实 provider key。
    # 这里通过模块入口执行，证明公开 CLI seam 可用，而不是脚本直接调用 loader。
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_harness.cli",
            "doctor",
            "--profile",
            "local",
            "--profiles-dir",
            str(PROFILES),
        ],
        check=False,
        cwd=ROOT,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert "profile: local" in result.stdout
    assert "storage: sqlite" in result.stdout
    assert "queue: in-memory" in result.stdout
    assert "observability sink: local-jsonl writable" in result.stdout
    assert "eval directory:" in result.stdout
    assert "(ok)" in result.stdout
    assert "model: fake (api key not required)" in result.stdout


def test_doctor_cli_reports_profile_errors_with_nonzero_exit(tmp_path: Path) -> None:
    """验证配置错误通过非零退出和字段路径暴露，便于自动化门禁精确定位。"""

    # 错误路径必须 non-zero 并带 field path；这比“打印 warning 后继续”更适合 smoke 门禁。
    (tmp_path / "broken.yaml").write_text(
        """
profile: broken
queue:
  kind: in-memory
observability:
  kind: local-jsonl
policy:
  provider: yaml
model:
  provider: fake
  requires_api_key: false
""",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_harness.cli",
            "doctor",
            "--profile",
            "broken",
            "--profiles-dir",
            str(tmp_path),
        ],
        check=False,
        cwd=ROOT,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 1
    assert "config.invalid" in result.stderr
    assert "field=storage" in result.stderr
