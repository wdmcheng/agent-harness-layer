"""EVL-004 acceptance、并发与安全错误 HTTP 合同。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from tests.contracts.auth_policy_hitl_contract_helpers import (
    asgi_request,
    sqlite_dsn,
    table_count,
)
from tests.contracts.test_eval_experiment_api_contracts import (
    BlockingEvaluator,
    ExplodingExperimentService,
    SplitAwareEvaluator,
    experiment_body,
    seed_approved_cases,
)

from agent_harness.events import LocalJsonlEventSink
from agent_harness.identity import IdentityContext
from agent_harness.runtime import RunOrchestrator
from app.main import create_app


@pytest.mark.asyncio
async def test_eval_experiment_accept_route_maps_version_and_policy_gates(
    tmp_path: Path,
) -> None:
    """验证接受路由分别映射版本不匹配、需审批与策略拒绝三类安全门禁。"""

    from agent_harness.auth import StaticTokenVerifier
    from agent_harness.evals import AcceptanceService, ExperimentService
    from agent_harness.policy import PolicyEngine, YamlPolicyProvider
    from agent_harness.storage import SQLAlchemyStorage, run_migrations

    db_path = tmp_path / "eval-experiment-api-gates.db"
    dsn = sqlite_dsn(db_path)
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    await seed_approved_cases(storage)
    body, candidate_id = experiment_body()
    baseline_id = cast(dict[str, str], body["baseline_harness_version"])["version_id"]
    experiments = ExperimentService(
        storage=storage,
        evaluator=SplitAwareEvaluator(baseline_id, candidate_id),
    )
    identity = IdentityContext(
        tenant_id="tenant-a",
        user_id="reviewer-a",
        session_id="session-a",
        permissions=["*"],
    )
    verifier = StaticTokenVerifier({"token-a": identity})
    allow_policy = PolicyEngine(provider=YamlPolicyProvider.default())
    allow_app = create_app(
        orchestrator=cast(RunOrchestrator, object()),
        event_sink=LocalJsonlEventSink(tmp_path / "gate-events.jsonl"),
        auth_verifier=verifier,
        policy_engine=allow_policy,
        experiment_service=experiments,
        acceptance_service=AcceptanceService(
            storage=storage, experiments=experiments, policy=allow_policy
        ),
    )
    headers = [
        (b"authorization", b"Bearer token-a"),
        (b"idempotency-key", b"gate-key"),
    ]
    try:
        created_status, created = await asgi_request(
            cast(Any, allow_app),
            method="POST",
            path="/api/v1/evals/experiments",
            body=body,
            headers=headers,
        )
        experiment_id = cast(str, created["experiment_id"])
        wrong_status, wrong = await asgi_request(
            cast(Any, allow_app),
            method="POST",
            path=f"/api/v1/evals/experiments/{experiment_id}/accept",
            body={
                "decision": "accepted",
                "reason": "wrong version must fail",
                "accepted_harness_version": "0" * 64,
            },
            headers=[(b"authorization", b"Bearer token-a")],
        )
        require_policy = PolicyEngine(
            provider=YamlPolicyProvider(require_approval_actions={"eval.harness.accept"})
        )
        require_app = create_app(
            orchestrator=cast(RunOrchestrator, object()),
            event_sink=LocalJsonlEventSink(tmp_path / "require-events.jsonl"),
            auth_verifier=verifier,
            policy_engine=require_policy,
            experiment_service=experiments,
            acceptance_service=AcceptanceService(
                storage=storage, experiments=experiments, policy=require_policy
            ),
        )
        require_status, require = await asgi_request(
            cast(Any, require_app),
            method="POST",
            path=f"/api/v1/evals/experiments/{experiment_id}/accept",
            body={
                "decision": "accepted",
                "reason": "policy gate",
                "accepted_harness_version": candidate_id,
            },
            headers=[(b"authorization", b"Bearer token-a")],
        )
        deny_policy = PolicyEngine(
            provider=YamlPolicyProvider(deny_actions={"eval.harness.accept"})
        )
        deny_app = create_app(
            orchestrator=cast(RunOrchestrator, object()),
            event_sink=LocalJsonlEventSink(tmp_path / "deny-events.jsonl"),
            auth_verifier=verifier,
            policy_engine=deny_policy,
            experiment_service=experiments,
            acceptance_service=AcceptanceService(
                storage=storage, experiments=experiments, policy=deny_policy
            ),
        )
        denied_status, denied = await asgi_request(
            cast(Any, deny_app),
            method="POST",
            path=f"/api/v1/evals/experiments/{experiment_id}/accept",
            body={
                "decision": "accepted",
                "reason": "policy deny gate",
                "accepted_harness_version": candidate_id,
            },
            headers=[(b"authorization", b"Bearer token-a")],
        )
    finally:
        await storage.dispose()

    assert created_status == 201
    assert wrong_status == 409
    assert wrong["error"]["code"] == "eval.experiment.accepted_version_mismatch"
    assert require_status == 409
    assert require["error"]["code"] == "eval.experiment.approval_required"
    assert denied_status == 403
    assert denied["error"]["code"] == "eval.experiment.policy_denied"
    assert table_count(db_path, "harness_acceptance_records") == 0
    assert table_count(db_path, "audit_logs") == 0


@pytest.mark.asyncio
async def test_concurrent_create_replay_does_not_duplicate_evaluator_side_effect(
    tmp_path: Path,
) -> None:
    """验证同幂等键的并发创建复用实验记录，且不会重复触发 evaluator 副作用。"""

    from agent_harness.auth import StaticTokenVerifier
    from agent_harness.evals import ExperimentService
    from agent_harness.storage import SQLAlchemyStorage, run_migrations

    db_path = tmp_path / "eval-experiment-api-concurrent.db"
    dsn = sqlite_dsn(db_path)
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    await seed_approved_cases(storage)
    body, candidate_id = experiment_body()
    baseline_id = cast(dict[str, str], body["baseline_harness_version"])["version_id"]
    evaluator = BlockingEvaluator(baseline_id, candidate_id)
    experiments = ExperimentService(storage=storage, evaluator=evaluator)
    identity = IdentityContext(
        tenant_id="tenant-a",
        user_id="creator",
        session_id="creator-session",
        permissions=["*"],
    )
    app = create_app(
        orchestrator=cast(RunOrchestrator, object()),
        event_sink=LocalJsonlEventSink(tmp_path / "concurrent-events.jsonl"),
        auth_verifier=StaticTokenVerifier({"token": identity}),
        experiment_service=experiments,
    )
    headers = [
        (b"authorization", b"Bearer token"),
        (b"idempotency-key", b"concurrent-key"),
    ]
    first_task = asyncio.create_task(
        asgi_request(
            cast(Any, app),
            method="POST",
            path="/api/v1/evals/experiments",
            body=body,
            headers=headers,
        )
    )
    try:
        await evaluator.started.wait()
        replay_status, replay = await asgi_request(
            cast(Any, app),
            method="POST",
            path="/api/v1/evals/experiments",
            body=body,
            headers=headers,
        )
        evaluator.release.set()
        first_status, first = await first_task
    finally:
        evaluator.release.set()
        if not first_task.done():
            await first_task
        await storage.dispose()

    assert first_status == 201
    assert replay_status == 200
    assert replay["experiment_id"] == first["experiment_id"]
    assert replay["status"] == "running"
    assert len(evaluator.calls) == 2
    assert table_count(db_path, "eval_experiments") == 1


def test_eval_experiment_internal_failure_uses_safe_500_envelope(tmp_path: Path) -> None:
    """验证未预期服务异常只返回稳定 500 包络，不回显内部异常或密钥。"""

    from agent_harness.auth import StaticTokenVerifier

    identity = IdentityContext(
        tenant_id="tenant-a",
        user_id="creator",
        session_id="creator-session",
        permissions=["*"],
    )
    service = ExplodingExperimentService()
    app = create_app(
        orchestrator=cast(RunOrchestrator, object()),
        event_sink=LocalJsonlEventSink(tmp_path / "error-events.jsonl"),
        auth_verifier=StaticTokenVerifier({"token": identity}),
        experiment_service=cast(Any, service),
    )
    body, _candidate_id = experiment_body()
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/api/v1/evals/experiments",
            json=body,
            headers={
                "Authorization": "Bearer token",
                "Idempotency-Key": "internal-error-key",
            },
        )
    status = response.status_code
    payload = response.json()

    assert status == 500
    assert payload["error"]["code"] == "api.internal_error"
    assert payload["error"]["message"] == "internal server error"
    assert "internal-provider-secret" not in json.dumps(payload)
    assert service.calls == 1
