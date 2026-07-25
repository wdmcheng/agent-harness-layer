"""复制模板后验证本地开发服务与示例 agent 的真实可运行表面。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_copied_template_runs_local_dev_and_generated_example() -> None:
    """从 workspace 外安装 wheel，并实际启动 dev 服务、生成和运行示例 agent。"""

    environment = os.environ.copy()
    environment.setdefault("NO_PROXY", "127.0.0.1,localhost")
    environment.setdefault("no_proxy", "127.0.0.1,localhost")
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "smoke_template_copy.py")],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=300,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "make-test=ok" in result.stdout
    assert "custom-make-test=ok" in result.stdout
    assert "make-quality=ok" in result.stdout
    assert "custom-environment=ok" in result.stdout
    assert "eval-ticket=migrated-and-passed" in result.stdout
    assert "health=ok profile=local" in result.stdout
    assert "swagger=offline-ok redoc=offline-ok" in result.stdout
    assert "swagger=online-pinned redoc=online-pinned" in result.stdout
    assert "list=ok run=completed approved-eval=passed" in result.stdout
    assert "smoke-template-copy: ok" in result.stdout
