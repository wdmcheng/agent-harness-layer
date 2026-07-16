"""Eval Gate migration、service、dataset 与 CLI 闭环合同测试。"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from tests.contracts.auth_policy_hitl_contract_helpers import (
    ROOT,
    sqlite_dsn,
    table_count,
    table_json_payloads,
)
from tests.contracts.run_trace_contract_helpers import seed_persisted_run

from agent_harness.events import LocalJsonlEventSink
from agent_harness.identity import IdentityContext
from agent_harness.observability import ProviderTelemetryAdapter, TelemetryStatus
from agent_harness.storage import SQLAlchemyStorage, run_migrations
from agent_harness.storage.run_trace_gate import StorageRunTraceResolver


class FailingScoreProvider(ProviderTelemetryAdapter):
    """外部 score provider 失败 fixture，验证本地 evidence 不被拖垮。"""

    provider_name = "score-provider"

    async def send(self, record: Any) -> TelemetryStatus:
        del record
        raise RuntimeError(
            "provider failed Authorization: Bearer score-secret-12345; "
            "Cookie: sessionid=score-cookie-12345"
        )


__all__ = [
    "Any",
    "FailingScoreProvider",
    "IdentityContext",
    "LocalJsonlEventSink",
    "Path",
    "ProviderTelemetryAdapter",
    "ROOT",
    "SQLAlchemyStorage",
    "StorageRunTraceResolver",
    "TelemetryStatus",
    "json",
    "pytest",
    "run_migrations",
    "seed_persisted_run",
    "sqlite3",
    "sqlite_dsn",
    "subprocess",
    "sys",
    "table_count",
    "table_json_payloads",
]
