"""复制模板后可直接运行的最小公开表面测试。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from fastapi.testclient import TestClient

from agent_harness.events import LocalJsonlEventSink
from agent_harness.registry import AgentRegistry
from app.main import create_app

TEMPLATE = Path(__file__).resolve().parents[1]


def test_local_health_uses_profile_summary_without_external_provider(tmp_path: Path) -> None:
    """local health 只需类型化配置，不建立外部依赖连接。"""

    app = create_app(
        orchestrator=cast(Any, object()),
        event_sink=LocalJsonlEventSink(tmp_path / "events.jsonl"),
        # 本测试只验证模板管理面；完整 registry/executor 组合由示例 agent
        # change 的运行合同收口，避免未完成的业务样例污染 health 基线。
        registry=AgentRegistry.load_from_directory(TEMPLATE / "agents" / "examples" / "basic"),
        approval_service=cast(Any, object()),
        eval_service=cast(Any, object()),
        profile="local",
        profiles_dir=TEMPLATE / "configs" / "profiles",
    )

    with TestClient(app) as client:
        response = client.get("/api/v1/health", headers={"X-Request-Id": "template-test"})

    assert response.status_code == 200
    assert response.json() == {
        "request_id": "template-test",
        "status": "ok",
        "profile": "local",
        "storage": {"kind": "sqlite", "status": "configured"},
        "queue": {"kind": "in-memory", "status": "configured"},
        "observability": {"kind": "local-jsonl", "status": "configured"},
    }
