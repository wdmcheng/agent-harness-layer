"""公开 fake run 时延门禁合同。"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path


def _smoke_module():
    """动态加载本地 smoke 脚本，直接验证其公开结果校验函数而不启动子进程。"""

    root = Path(__file__).parents[2]
    path = root / "scripts" / "smoke_local.py"
    spec = importlib.util.spec_from_file_location("agent_harness_smoke_local", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fake_run_latency_gate_fails_closed_over_fixed_threshold() -> None:
    """验证 fake run 超过固定延迟预算时 smoke 门禁返回失败，防止性能回归被掩盖。"""

    smoke = _smoke_module()
    result = subprocess.CompletedProcess(args=["fake-run"], returncode=0, stdout="", stderr="")
    events = [
        {
            "event_type": "model.usage.updated",
            "terminal": False,
        },
        {
            "event_type": "run.completed",
            "terminal": True,
        },
    ]

    assert (
        smoke.validate_fake_run_result(
            result=result,
            events=events,
            elapsed_seconds=5.001,
        )
        == 1
    )
