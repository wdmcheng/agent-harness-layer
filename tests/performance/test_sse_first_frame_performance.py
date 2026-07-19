"""本地 fake 配置下 SSE 首个业务帧的固定性能门禁。"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from pathlib import Path
from time import perf_counter

from fastapi.testclient import TestClient

from agent_harness.storage import run_migrations
from app.main import create_app

ROOT = Path(__file__).resolve().parents[2]
SERVICE_APP = ROOT / "templates" / "service-app"
SAMPLE_COUNT = 30
MAX_P95_MILLISECONDS = 1_000.0


def _percentile_95(samples: list[float]) -> float:
    """按 nearest-rank 计算固定样本 P95，避免统计库插值规则漂移。"""

    return sorted(samples)[math.ceil(len(samples) * 0.95) - 1]


def test_local_fake_run_sse_first_frame_p95_is_below_one_second(tmp_path: Path) -> None:
    """从 HTTP 分发计时至首个业务帧，验证端到端而非格式化函数耗时。"""

    dsn = f"sqlite+aiosqlite:///{tmp_path / 'first-frame.db'}"
    events_path = tmp_path / "events.jsonl"
    run_migrations(dsn)
    environment = {
        **os.environ,
        "AGENT_HARNESS_BUDGET__FINGERPRINT_KEY": "sse-p95-ephemeral-key",
    }
    # 先用真实 CLI 生成终态运行和事件文件，避免首帧测试只覆盖空流路径。
    created = subprocess.run(
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
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert created.returncode == 0, created.stderr
    # 性能样本共用同一份已存在的事件，排除模型执行和持久化写入对读取延迟的干扰。
    preloaded = [
        json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines() if line
    ]
    run_ids = {str(item["run_id"]) for item in preloaded}
    assert len(run_ids) == 1
    run_id = run_ids.pop()

    app = create_app(
        profile="local",
        profiles_dir=SERVICE_APP / "configs" / "profiles",
        storage_dsn=dsn,
        events_path=events_path,
    )
    samples_ms: list[float] = []
    with TestClient(app) as client:
        for sample_index in range(SAMPLE_COUNT):
            # 计时边界从 HTTP 请求发出开始，到客户端收到首个业务字节为止。
            started = perf_counter()
            with client.stream(
                "GET",
                f"/api/v1/runs/{run_id}/events/stream",
                headers={
                    "Accept": "text/event-stream",
                    "X-Request-Id": f"req-p95-{sample_index}",
                },
            ) as response:
                first_chunk = next(response.iter_raw())
                elapsed_ms = (perf_counter() - started) * 1_000
            assert response.status_code == 200
            assert first_chunk.startswith(b"id: ")
            samples_ms.append(elapsed_ms)

    # P95 为统计学百分位指标，不表示开发阶段；它用来抑制单次偶发抖动。
    p95_ms = _percentile_95(samples_ms)
    print(
        "sse-first-frame: "
        f"profile=local model=fake preloaded_events={len(preloaded)} "
        f"samples={SAMPLE_COUNT} "
        "boundary=http_dispatch_to_first_business_frame "
        f"min_ms={min(samples_ms):.3f} p95_ms={p95_ms:.3f} "
        f"max_ms={max(samples_ms):.3f} threshold_ms={MAX_P95_MILLISECONDS:.0f}"
    )
    assert p95_ms < MAX_P95_MILLISECONDS
