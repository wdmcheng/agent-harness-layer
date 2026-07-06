"""Phase 2 vendor boundary 和 doctor CLI seam 的契约测试。

vendor allowlist 的事实来源在 `agent_harness.contracts.boundaries`；这些测试只确认
公开声明会被质量门禁和 CLI smoke 共同使用，不在测试里复制第二份规则。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from agent_harness.contracts.boundaries import (
    BANNED_VENDOR_IMPORTS,
    is_vendor_import_allowed,
)

ROOT = Path(__file__).resolve().parents[2]
PROFILES = ROOT / "templates" / "service-app" / "configs" / "profiles"


def test_boundary_contract_lists_banned_vendors_and_adapter_allowlist() -> None:
    # allowlist 按目录职责判断，不按单个文件白名单判断，方便后续 adapter 扩展。
    assert {"pydantic_ai", "dbos", "logfire", "phoenix", "langfuse"} <= BANNED_VENDOR_IMPORTS
    assert is_vendor_import_allowed(
        ROOT / "packages" / "agent-harness" / "src" / "agent_harness" / "adapters" / "model.py"
    )
    assert not is_vendor_import_allowed(
        ROOT / "templates" / "service-app" / "app" / "api" / "run.py"
    )


def test_doctor_cli_reports_local_profile_without_provider_keys() -> None:
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
