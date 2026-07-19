"""Observability provider adapter 的公开契约测试。

这些测试只穿过稳定公共 seam：TelemetryFacade、trace context DTO、
provider adapter contract、typed config 和 doctor CLI。它们不要求真实 SaaS 账号，
也不声明 eval case / score workflow 已经完成。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from tests.contracts.auth_policy_hitl_contract_helpers import sqlite_dsn
from tests.contracts.run_trace_contract_helpers import seed_persisted_run

from agent_harness.artifacts import FileArtifactStore
from agent_harness.config import load_settings
from agent_harness.events import CanonicalEventType, EventBus, LocalJsonlEventSink
from agent_harness.observability import (
    OTelTelemetryAdapter,
    ProviderTelemetryAdapter,
    TelemetryContext,
    TelemetryFacade,
    TelemetryRecord,
    TelemetryStatus,
    redact_telemetry_payload,
)
from agent_harness.storage import SQLAlchemyStorage, run_migrations
from agent_harness.storage.run_trace_gate import StorageRunTraceResolver

ROOT = Path(__file__).resolve().parents[2]
PROFILES = ROOT / "templates" / "service-app" / "configs" / "profiles"


class RecordingProviderAdapter(ProviderTelemetryAdapter):
    """测试用 provider adapter，记录 facade fan-out 前收到的脱敏 DTO。"""

    provider_name = "recording"

    def __init__(self, *, fail_with: Exception | None = None) -> None:
        """初始化脱敏记录列表，并可选配置确定性 provider 失败以覆盖降级路径。"""

        self.records: list[TelemetryRecord] = []
        self.fail_with = fail_with

    async def send(self, record: TelemetryRecord) -> TelemetryStatus:
        """保存 facade 已处理的 DTO；配置失败时在 provider 边界抛出异常。"""

        self.records.append(record)
        if self.fail_with is not None:
            raise self.fail_with
        return TelemetryStatus(provider=self.provider_name, status="sent")


def usage_payload(*, run_id: str, trace_id: str) -> dict[str, object]:
    """构造 Facade canonical usage 合同使用的完整统一 DTO。"""

    return {
        "usage_kind": "model",
        "tenant_id": "default",
        "provider": "fake",
        "model": "fake-basic",
        "input_tokens": 1,
        "output_tokens": 2,
        "cost_usd": None,
        "cost_status": "unavailable",
        "latency_ms": 3,
        "decision": {"provider_called": True},
        "run_id": run_id,
        "agent_id": "agent-1",
        "request_id": None,
        "trace_id": trace_id,
    }


__all__ = [
    "Any",
    "CanonicalEventType",
    "EventBus",
    "FileArtifactStore",
    "LocalJsonlEventSink",
    "OTelTelemetryAdapter",
    "PROFILES",
    "Path",
    "ProviderTelemetryAdapter",
    "ROOT",
    "RecordingProviderAdapter",
    "SQLAlchemyStorage",
    "StorageRunTraceResolver",
    "TelemetryContext",
    "TelemetryFacade",
    "TelemetryRecord",
    "TelemetryStatus",
    "json",
    "load_settings",
    "pytest",
    "redact_telemetry_payload",
    "run_migrations",
    "seed_persisted_run",
    "sqlite_dsn",
    "subprocess",
    "sys",
    "usage_payload",
]
