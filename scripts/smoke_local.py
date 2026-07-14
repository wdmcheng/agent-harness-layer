"""验证 workspace 和 service-app shell 的本地 smoke。

本脚本只走 local profile 和公开 CLI seam，证明当前 shell 不需要真实模型 key、
外部 observability provider、数据库或队列服务。任何需要连接外部服务的检查都应留给
后续 service smoke，避免本地 smoke 写入用户环境或依赖机器状态。
"""

from __future__ import annotations

import importlib
import subprocess
import sys
import tempfile
from pathlib import Path

from agent_harness.config import load_settings
from agent_harness.storage import run_migrations

ROOT = Path(__file__).resolve().parents[1]
SERVICE_APP = ROOT / "templates" / "service-app"


REQUIRED_TEMPLATE_PATHS = [
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


def _fail(message: str) -> int:
    print(f"smoke-local: {message}", file=sys.stderr)
    return 1


def check_import() -> int:
    """通过安装后的 package seam 验证核心包可 import 并暴露版本。"""

    module = importlib.import_module("agent_harness")
    version = getattr(module, "__version__", "")
    if not isinstance(version, str) or not version:
        return _fail("agent_harness.__version__ is missing.")
    return 0


def check_template_layout() -> int:
    """锁住 template shell 的目录入口，不声称这些预留目录已实现业务能力。"""

    missing = [path for path in REQUIRED_TEMPLATE_PATHS if not (SERVICE_APP / path).exists()]
    if missing:
        return _fail(f"missing template paths: {', '.join(missing)}")
    return 0


def check_local_profile() -> int:
    """确认 local profile 保持离线可跑，不能偷偷要求真实 provider key。"""

    settings = load_settings(profile="local", profiles_dir=SERVICE_APP / "configs" / "profiles")
    if settings.profile != "local":
        return _fail("local profile must declare profile=local.")
    if settings.model.requires_api_key:
        return _fail("local profile must not require real provider keys.")
    return 0


def check_doctor() -> int:
    """运行 doctor CLI，验证 local profile 诊断路径保持只读。"""

    # 通过模块入口运行 doctor，避免 console script 安装状态掩盖 CLI seam 问题。
    # doctor 设计为只读诊断；本 smoke 不允许它初始化数据库、队列或外部 provider。
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_harness.cli",
            "doctor",
            "--profile",
            "local",
            "--profiles-dir",
            str(SERVICE_APP / "configs" / "profiles"),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return _fail(f"doctor failed: {result.stderr.strip()}")
    if "profile: local" not in result.stdout:
        return _fail("doctor output did not report local profile.")
    return 0


def check_agents_list() -> int:
    """确认 local registry smoke agent 可离线枚举。"""

    with tempfile.TemporaryDirectory(prefix="agent-harness-agents-list-") as directory:
        dsn = f"sqlite+aiosqlite:///{Path(directory) / 'agents-list.db'}"
        # smoke setup 显式迁移隔离数据库；被测 agents list 本身仍只读校验 head。
        run_migrations(dsn)
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "agent_harness.cli",
                "agents",
                "list",
                "--agents-dir",
                str(SERVICE_APP / "agents"),
                "--profiles-dir",
                str(SERVICE_APP / "configs" / "profiles"),
                "--storage-dsn",
                dsn,
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
    if result.returncode != 0:
        return _fail(f"agents list failed: {result.stderr.strip()}")
    if "examples.basic" not in result.stdout:
        return _fail("agents list did not report examples.basic.")
    return 0


def main() -> int:
    """依次运行 local smoke 检查。"""

    checks = [
        check_import,
        check_template_layout,
        check_local_profile,
        check_doctor,
        check_agents_list,
    ]
    for check in checks:
        result = check()
        if result != 0:
            return result
    print("smoke-local: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
