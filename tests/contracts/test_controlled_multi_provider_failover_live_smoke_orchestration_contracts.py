"""多供应商 failover live producer 后置编排失败合同。"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Protocol, cast

import pytest
from tests.contracts.controlled_multi_provider_failover_test_support import (
    ROUTE_A,
    ROUTE_B,
    bound_failover_invocation,
)

from agent_harness.identity import IdentityContext
from agent_harness.models import ModelRequest
from agent_harness.storage.adapters.sqlalchemy import SQLAlchemyStorage


class _RunAwareExecutor(Protocol):
    """后置编排失败测试只依赖 executor 已绑定的正式 run identity。"""

    run_id: str | None


@pytest.mark.asyncio
async def test_run_authorized_recovers_durable_chain_after_terminal_orchestration_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """provider 已结算后 terminal 编排失败仍必须从 run identity 回读耐久调用事实。"""

    fixture = await bound_failover_invocation(
        tmp_path,
        route_count=2,
        scripts={
            ROUTE_A["deployment_id"]: ["client_not_started"],
            ROUTE_B["deployment_id"]: ["completed"],
        },
    )
    try:
        await fixture.bound.complete(
            ModelRequest(prompt="terminal failure", max_output_tokens=8),
            operation_key=fixture.operation_key,
        )
        producer = importlib.import_module("scripts.smoke_live_model_failover")
        identity = IdentityContext(
            tenant_id="tenant-a",
            user_id="user-a",
            session_id="session-a",
        )
        settings = SimpleNamespace(
            identity=SimpleNamespace(default=identity),
            budget=SimpleNamespace(max_tokens_per_run=100, max_cost_usd_per_run=1),
            model=SimpleNamespace(
                deployments={
                    ROUTE_A["deployment_id"]: SimpleNamespace(
                        max_output_tokens=8,
                        max_attempts=1,
                    ),
                    ROUTE_B["deployment_id"]: SimpleNamespace(
                        max_output_tokens=8,
                        max_attempts=1,
                    ),
                }
            ),
        )
        resolved = {
            ROUTE_A["deployment_id"]: SimpleNamespace(
                deployment_id=ROUTE_A["deployment_id"],
                provider_kind="openai-compatible",
                default_model=ROUTE_A["model_id"],
                credential_ref="primary_key",
                endpoint_origin="https://primary.example.test",
            ),
            ROUTE_B["deployment_id"]: SimpleNamespace(
                deployment_id=ROUTE_B["deployment_id"],
                provider_kind="openai-compatible",
                default_model=ROUTE_B["model_id"],
                credential_ref="secondary_key",
                endpoint_origin="https://secondary.example.test",
            ),
        }

        class TerminalFailingOrchestrator:
            """模拟 executor 完成且 durable settlement 已存在，但 terminal 发布随后失败。"""

            def __init__(self, **values: object) -> None:
                resolver = cast(
                    Callable[[str], _RunAwareExecutor],
                    values["executor_resolver"],
                )
                self.executor = resolver("system.live_model_failover_smoke")

            async def start_run(self, **_values: object) -> None:
                self.executor.run_id = fixture.run_id
                raise RuntimeError("terminal publication failed")

        async def close_services(_services: object) -> None:
            return None

        def load_settings(**_values: object) -> SimpleNamespace:
            return settings

        def usage_call_id(**_values: object) -> str:
            return fixture.usage_call_id

        def resolve_deployment(_model: object, deployment_id: str) -> SimpleNamespace:
            return resolved[deployment_id]

        def migrate(_dsn: str) -> None:
            return None

        def storage_from_dsn(_cls: type[object], _dsn: str) -> SQLAlchemyStorage:
            return fixture.storage

        def build_services(**_values: object) -> object:
            return object()

        monkeypatch.setenv(
            producer.DEPLOYMENTS_ENV,
            f"{ROUTE_A['deployment_id']},{ROUTE_B['deployment_id']}",
        )
        monkeypatch.setattr(producer, "load_settings", load_settings)
        monkeypatch.setattr(
            producer,
            "stable_usage_call_id",
            usage_call_id,
        )
        monkeypatch.setattr(
            producer,
            "resolve_model_deployment",
            resolve_deployment,
        )
        monkeypatch.setattr(producer, "run_migrations", migrate)
        monkeypatch.setattr(
            producer.SQLAlchemyStorage,
            "from_dsn",
            classmethod(storage_from_dsn),
        )
        monkeypatch.setattr(
            producer,
            "build_agent_execution_services",
            build_services,
        )
        monkeypatch.setattr(producer, "close_agent_execution_services", close_services)
        monkeypatch.setattr(producer, "RunOrchestrator", TerminalFailingOrchestrator)

        payload, exit_code = await producer.run_authorized(
            profile="service",
            profiles_dir=tmp_path,
            secret_root=tmp_path,
        )

        assert exit_code == 1
        assert payload["status"] == "failed"
        assert payload["reason_code"] == "contract_failure"
        assert payload["provider_called"] is True
        assert payload["attempt_count"] == 2
        assert payload["chain_id"] is not None
        assert [item["outcome"] for item in payload["candidates"]] == [
            "not_started",
            "completed",
        ]
    finally:
        await fixture.storage.dispose()
