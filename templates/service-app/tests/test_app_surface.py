"""复制模板后可直接运行的最小公开表面测试。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from fastapi.testclient import TestClient

from agent_harness.events import LocalJsonlEventSink
from agent_harness.registry import AgentRegistry
from app.main import create_app

APP_ROOT = Path(__file__).resolve().parents[1]


def test_local_health_uses_profile_summary_without_external_provider(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """local health 只需类型化配置，不建立外部依赖连接。"""

    # fingerprint key 是设置加载的 fail-closed 前置条件；测试值只用于本进程，
    # 不写入模板配置，也不允许调用方环境中的 `_FILE` 形成冲突。
    monkeypatch.setenv(
        "AGENT_HARNESS_BUDGET__FINGERPRINT_KEY",
        "test-only-template-health-fingerprint-key",
    )
    monkeypatch.delenv("AGENT_HARNESS_BUDGET__FINGERPRINT_KEY_FILE", raising=False)
    app = create_app(
        orchestrator=cast(Any, object()),
        event_sink=LocalJsonlEventSink(tmp_path / "events.jsonl"),
        # App factory 需要合法 registry，但 health 不执行 Agent。这里仍从完整 agents
        # 根加载，确保 dotted schema ref 的首段与 registry root 一致。
        registry=AgentRegistry.load_from_directory(APP_ROOT / "agents"),
        approval_service=cast(Any, object()),
        eval_service=cast(Any, object()),
        profile="local",
        profiles_dir=APP_ROOT / "configs" / "profiles",
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
