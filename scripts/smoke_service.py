"""在 workspace 外复制模板，用已构建 wheel 运行四服务 Compose smoke。"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "templates" / "service-app"


def parse_args() -> argparse.Namespace:
    """解析服务冒烟模式；迁移专用模式仍复用同一复制模板边界。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--migrate-only", action="store_true")
    return parser.parse_args()


def _build_core_wheel() -> Path:
    """构建唯一核心 wheel，并拒绝残留或缺失产物以防复制 smoke 使用错误版本。"""

    subprocess.run(
        ["uv", "build", "--package", "agent-harness", "--clear"],
        cwd=ROOT,
        check=True,
    )
    wheels = sorted((ROOT / "dist").glob("agent_harness-*.whl"))
    if len(wheels) != 1:
        raise RuntimeError(f"expected one core wheel, found {len(wheels)}")
    return wheels[0]


def _run_copied_smoke(command: list[str], copied: Path, wheel_target: Path) -> None:
    """中断时等待子 smoke 完成 finally，避免临时目录先于 Compose cleanup 消失。"""

    process = subprocess.Popen(
        command,
        cwd=copied,
        env={**os.environ, "AGENT_HARNESS_SOURCE": str(wheel_target)},
        start_new_session=True,
    )
    try:
        return_code = process.wait()
    except KeyboardInterrupt:
        os.killpg(process.pid, signal.SIGINT)
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=10)
        raise
    if return_code != 0:
        raise subprocess.CalledProcessError(return_code, command)


def main() -> int:
    """在临时工作区复制模板、注入刚构建 wheel 并运行受限服务冒烟。"""

    args = parse_args()
    wheel = _build_core_wheel()
    with tempfile.TemporaryDirectory(prefix="agent-harness-service-smoke-") as temp:
        copied = Path(temp) / "service-app"
        # 复制时排除本机状态与缓存，确保被测模板不能依赖宿主已安装环境或历史密钥。
        shutil.copytree(
            TEMPLATE,
            copied,
            ignore=shutil.ignore_patterns(
                ".agent-harness",
                ".venv",
                ".ruff_cache",
                "__pycache__",
            ),
        )
        wheel_target = copied / ".agent-harness" / wheel.name
        wheel_target.parent.mkdir(parents=True)
        shutil.copy2(wheel, wheel_target)
        command = ["make", "smoke-service", f"PYTHON={sys.executable}"]
        if args.migrate_only:
            command = [sys.executable, "scripts/smoke_service.py", "--migrate-only"]
        try:
            _run_copied_smoke(command, copied, wheel_target)
        except subprocess.CalledProcessError:
            return 1
        if list(copied.rglob("*.secret")):
            raise RuntimeError("service smoke did not clean temporary secret files")
    print("smoke-service-root: workspace-outside=ok wheel-only=ok secret-cleanup=ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
