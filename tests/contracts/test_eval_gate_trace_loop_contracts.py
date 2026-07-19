"""Eval Gate 与 trace/eval 闭环的 API、策略和 provider 合同测试。

这些用例只穿过公开 seam：OpenSpec artifact、`agent_harness.evals`
DTO/service、Repository/UoW、ScoreSink、template API 和 CLI/Makefile 入口。
它们刻意避免直接操作 SQLAlchemy session，防止 eval runner 绕过 storage 边界。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, cast

import pytest
from tests.contracts.auth_policy_hitl_contract_helpers import (
    ROOT,
    asgi_request,
    descriptor,
    sqlite_dsn,
    table_count,
)
from tests.contracts.run_trace_contract_helpers import seed_persisted_run

from agent_harness.events import LocalJsonlEventSink
from agent_harness.identity import IdentityContext
from agent_harness.observability import (
    ProviderTelemetryAdapter,
    TelemetryStatus,
)
from agent_harness.policy import PolicyCheck, PolicyEngine, PolicyEvaluation
from agent_harness.registry import AgentRegistry
from agent_harness.storage import SQLAlchemyStorage, run_migrations

PROFILES = ROOT / "templates" / "service-app" / "configs" / "profiles"


class FailingScoreProvider(ProviderTelemetryAdapter):
    """ScoreSink provider failure fixture，验证 local evidence 不被外部错误拖垮。"""

    provider_name = "score-provider"

    async def send(self, record: Any) -> TelemetryStatus:
        """返回含敏感片段的受控失败，验证 ScoreSink 仅保留本地证据且对外诊断会脱敏。"""

        raise RuntimeError(
            "provider failed Authorization: Bearer score-secret-12345; "
            "Cookie: sessionid=score-cookie-12345"
        )


class RecordingEvalPolicyProvider:
    """记录 EVL-002 policy check，并按测试指定决策返回。"""

    def __init__(self, decision: str = "allow") -> None:
        """保存预置策略决定和收到的 check，供 API/审批用例验证 policy 输入与调用次数。"""

        self.decision = decision
        self.checks: list[PolicyCheck] = []

    async def evaluate(self, check: PolicyCheck) -> PolicyEvaluation:
        """记录 check 并返回匹配 actor/action/resource 的决定，隔离真实策略后端。"""

        self.checks.append(check)
        return PolicyEvaluation(
            decision=self.decision,
            reason=f"eval approve {self.decision}",
            actor=check.actor,
            action=check.action,
            resource=check.resource,
            metadata={"context": check.context},
        )


__all__ = [
    "AgentRegistry",
    "Any",
    "FailingScoreProvider",
    "IdentityContext",
    "LocalJsonlEventSink",
    "PROFILES",
    "Path",
    "PolicyCheck",
    "PolicyEngine",
    "PolicyEvaluation",
    "ProviderTelemetryAdapter",
    "ROOT",
    "RecordingEvalPolicyProvider",
    "SQLAlchemyStorage",
    "TelemetryStatus",
    "asgi_request",
    "cast",
    "descriptor",
    "json",
    "pytest",
    "run_migrations",
    "seed_persisted_run",
    "sqlite3",
    "sqlite_dsn",
    "table_count",
]
