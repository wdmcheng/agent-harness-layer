"""验证 workspace 和 service-app shell 的本地 smoke。

本脚本只走 local profile 和公开 CLI seam，证明当前 shell 不需要真实模型 key、
外部 observability provider、数据库或队列服务。任何需要连接外部服务的检查都应留给
后续 service smoke，避免本地 smoke 写入用户环境或依赖机器状态。
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from time import perf_counter

from agent_harness.config import load_settings
from agent_harness.storage import run_migrations

ROOT = Path(__file__).resolve().parents[1]
SERVICE_APP = ROOT / "templates" / "service-app"
SMOKE_FINGERPRINT_KEY = "local-smoke-ephemeral-key"


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


def _smoke_env() -> dict[str, str]:
    """只向 smoke 子进程注入临时 key，不写 profile、dotenv 或状态目录。"""

    return {
        **os.environ,
        "AGENT_HARNESS_BUDGET__FINGERPRINT_KEY": SMOKE_FINGERPRINT_KEY,
    }


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

    settings = load_settings(
        profile="local",
        profiles_dir=SERVICE_APP / "configs" / "profiles",
        overrides={"budget": {"fingerprint_key": SMOKE_FINGERPRINT_KEY}},
    )
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
        env=_smoke_env(),
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
            env=_smoke_env(),
            check=False,
            capture_output=True,
            text=True,
        )
    if result.returncode != 0:
        return _fail(f"agents list failed: {result.stderr.strip()}")
    if "examples.basic" not in result.stdout:
        return _fail("agents list did not report examples.basic.")
    return 0


def validate_fake_run_result(
    *,
    result: subprocess.CompletedProcess[str],
    events: list[dict[str, object]],
    elapsed_seconds: float,
    max_seconds: float = 5.0,
) -> int:
    """验证公开 run 输出、唯一 terminal/usage 与固定入口时延门禁。"""

    if result.returncode != 0:
        return _fail(f"fake run failed: {result.stderr.strip()}")
    if elapsed_seconds >= max_seconds:
        return _fail(
            f"fake run latency {elapsed_seconds:.3f}s exceeds fixed {max_seconds:.3f}s gate"
        )
    terminals = [item for item in events if item.get("terminal") is True]
    usage = [item for item in events if item.get("event_type") == "model.usage.updated"]
    if len(terminals) != 1:
        return _fail(f"fake run expected one terminal event, got {len(terminals)}")
    if len(usage) != 1:
        return _fail(f"fake run expected one final usage event, got {len(usage)}")
    if usage[0].get("terminal") is not False:
        return _fail("model.usage.updated must not close the run stream")
    return 0


def check_fake_run() -> int:
    """从公开 CLI 入口运行固定 fake model，并计时到唯一 terminal。"""

    with tempfile.TemporaryDirectory(prefix="agent-harness-fake-run-") as directory:
        state_dir = Path(directory)
        dsn = f"sqlite+aiosqlite:///{state_dir / 'fake-run.db'}"
        events_path = state_dir / "events.jsonl"
        run_migrations(dsn)
        started = perf_counter()
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "agent_harness.cli",
                "run",
                "examples.ticket_triage",
                "--profile",
                "local",
                "--profiles-dir",
                str(SERVICE_APP / "configs" / "profiles"),
                "--agents-dir",
                str(SERVICE_APP / "agents"),
                "--storage-dsn",
                dsn,
                "--events-path",
                str(events_path),
                "--prompt",
                "billing invoice needs review",
            ],
            cwd=ROOT,
            env=_smoke_env(),
            check=False,
            capture_output=True,
            text=True,
        )
        elapsed_seconds = perf_counter() - started
        events = (
            [
                json.loads(line)
                for line in events_path.read_text(encoding="utf-8").splitlines()
                if line
            ]
            if events_path.exists()
            else []
        )
        checked = validate_fake_run_result(
            result=result,
            events=events,
            elapsed_seconds=elapsed_seconds,
        )
        if checked != 0:
            return checked
        terminal = next(item for item in events if item.get("terminal") is True)
        usage = next(item for item in events if item.get("event_type") == "model.usage.updated")
        correlation = usage.get("payload", {}).get("correlation", {})
        print(
            "smoke-local: fake_run "
            f"elapsed_seconds={elapsed_seconds:.3f} "
            f"run_id={terminal.get('run_id')} "
            f"trace_id={terminal.get('trace_id')} "
            f"usage_call_id={correlation.get('usage_call_id')}"
        )
    return 0


def main() -> int:
    """依次运行 local smoke 检查。"""

    checks = [
        check_import,
        check_template_layout,
        check_local_profile,
        check_doctor,
        check_agents_list,
        check_fake_run,
    ]
    for check in checks:
        result = check()
        if result != 0:
            return result
    print("smoke-local: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
