"""评测实验 API 规范化、租户隔离与幂等合同测试。"""

from __future__ import annotations

from tests.contracts.test_eval_experiment_api_contracts import (
    Any as Any,
)
from tests.contracts.test_eval_experiment_api_contracts import (
    FailingPublisher as FailingPublisher,
)
from tests.contracts.test_eval_experiment_api_contracts import (
    IdentityContext as IdentityContext,
)
from tests.contracts.test_eval_experiment_api_contracts import (
    LocalJsonlEventSink as LocalJsonlEventSink,
)
from tests.contracts.test_eval_experiment_api_contracts import (
    Path as Path,
)
from tests.contracts.test_eval_experiment_api_contracts import (
    RunOrchestrator as RunOrchestrator,
)
from tests.contracts.test_eval_experiment_api_contracts import (
    SplitAwareEvaluator as SplitAwareEvaluator,
)
from tests.contracts.test_eval_experiment_api_contracts import (
    UnsafePublisher as UnsafePublisher,
)
from tests.contracts.test_eval_experiment_api_contracts import (
    asgi_request as asgi_request,
)
from tests.contracts.test_eval_experiment_api_contracts import (
    cast as cast,
)
from tests.contracts.test_eval_experiment_api_contracts import (
    create_app as create_app,
)
from tests.contracts.test_eval_experiment_api_contracts import (
    experiment_body as experiment_body,
)
from tests.contracts.test_eval_experiment_api_contracts import (
    json as json,
)
from tests.contracts.test_eval_experiment_api_contracts import (
    pytest as pytest,
)
from tests.contracts.test_eval_experiment_api_contracts import (
    seed_approved_cases as seed_approved_cases,
)
from tests.contracts.test_eval_experiment_api_contracts import (
    sqlite_dsn as sqlite_dsn,
)
from tests.contracts.test_eval_experiment_api_contracts import (
    table_count as table_count,
)


def test_public_create_body_normalizes_tag_order_and_duplicates() -> None:
    """验证公开创建 DTO 将 tag 视为去重、顺序无关的幂等输入集合。"""

    from agent_harness.evals import ExperimentCreateBody

    body, _candidate_id = experiment_body()
    first = ExperimentCreateBody.model_validate(
        {**body, "tags": ["tool_selection", "retrieval_quality"]}
    )
    second = ExperimentCreateBody.model_validate(
        {
            **body,
            "tags": [
                "retrieval_quality",
                "tool_selection",
                "retrieval_quality",
            ],
        }
    )
    assert first.to_payload() == second.to_payload()


def test_public_create_body_normalizes_regression_policy_set_order() -> None:
    """验证回归策略中的 case 集合规范化后不受客户端提交顺序影响。"""

    from agent_harness.evals import ExperimentCreateBody

    body, _candidate_id = experiment_body()
    first = ExperimentCreateBody.model_validate(
        {
            **body,
            "regression_policy": {
                "case_ids": ["case-b", "case-a"],
                "critical_case_ids": ["case-d", "case-c"],
            },
        }
    )
    second = ExperimentCreateBody.model_validate(
        {
            **body,
            "regression_policy": {
                "case_ids": ["case-a", "case-b"],
                "critical_case_ids": ["case-c", "case-d"],
            },
        }
    )
    assert first.to_payload() == second.to_payload()


@pytest.mark.asyncio
async def test_eval_experiment_http_loop_is_tenant_safe_and_idempotent(
    tmp_path: Path,
) -> None:
    """验证实验 HTTP 生命周期同时保持租户隔离、幂等重放与安全错误边界。"""

    from agent_harness.auth import StaticTokenVerifier
    from agent_harness.evals import AcceptanceService, ExperimentService
    from agent_harness.policy import PolicyEngine, YamlPolicyProvider
    from agent_harness.storage import SQLAlchemyStorage, run_migrations

    db_path = tmp_path / "eval-experiment-api.db"
    dsn = sqlite_dsn(db_path)
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    await seed_approved_cases(storage)
    body, candidate_id = experiment_body()
    baseline_id = cast(dict[str, str], body["baseline_harness_version"])["version_id"]
    evaluator = SplitAwareEvaluator(baseline_id, candidate_id)
    experiments = ExperimentService(
        storage=storage,
        evaluator=evaluator,
        publishers=[FailingPublisher(), UnsafePublisher()],
    )
    policy = PolicyEngine(provider=YamlPolicyProvider.default())
    acceptance = AcceptanceService(
        storage=storage,
        experiments=experiments,
        policy=policy,
    )
    tenant_a = IdentityContext(
        tenant_id="tenant-a",
        user_id="reviewer-a",
        session_id="session-a",
        permissions=["*"],
    )
    tenant_b = tenant_a.model_copy(update={"tenant_id": "tenant-b", "user_id": "reviewer-b"})
    low_privilege = tenant_a.model_copy(
        update={"user_id": "reader-without-permission", "permissions": []}
    )
    app = create_app(
        orchestrator=cast(RunOrchestrator, object()),
        event_sink=LocalJsonlEventSink(tmp_path / "events.jsonl"),
        auth_verifier=StaticTokenVerifier(
            {"token-a": tenant_a, "token-b": tenant_b, "token-low": low_privilege}
        ),
        policy_engine=policy,
        experiment_service=experiments,
        acceptance_service=acceptance,
    )
    create_headers = [
        (b"authorization", b"Bearer token-a"),
        (b"idempotency-key", b"experiment-key"),
    ]
    try:
        created_status, created = await asgi_request(
            cast(Any, app),
            method="POST",
            path="/api/v1/evals/experiments",
            body=body,
            headers=create_headers,
        )
        replay_status, replay = await asgi_request(
            cast(Any, app),
            method="POST",
            path="/api/v1/evals/experiments",
            body={**body, "tags": ["tool_selection", "tool_selection"]},
            headers=create_headers,
        )
        experiment_id = cast(str, created["experiment_id"])
        read_status, read = await asgi_request(
            cast(Any, app),
            method="GET",
            path=f"/api/v1/evals/experiments/{experiment_id}",
            headers=[(b"authorization", b"Bearer token-a")],
        )
        comparison_status, comparison = await asgi_request(
            cast(Any, app),
            method="GET",
            path=f"/api/v1/evals/experiments/{experiment_id}/comparison",
            headers=[(b"authorization", b"Bearer token-a")],
        )
        accept_body = {
            "decision": "accepted",
            "reason": "manual evidence review passed",
            "accepted_harness_version": candidate_id,
        }
        accepted_status, accepted = await asgi_request(
            cast(Any, app),
            method="POST",
            path=f"/api/v1/evals/experiments/{experiment_id}/accept",
            body=accept_body,
            headers=[(b"authorization", b"Bearer token-a")],
        )
        accepted_replay_status, accepted_replay = await asgi_request(
            cast(Any, app),
            method="POST",
            path=f"/api/v1/evals/experiments/{experiment_id}/accept",
            body=accept_body,
            headers=[(b"authorization", b"Bearer token-a")],
        )
        cross_status, cross = await asgi_request(
            cast(Any, app),
            method="GET",
            path=f"/api/v1/evals/experiments/{experiment_id}",
            headers=[(b"authorization", b"Bearer token-b")],
        )
        conflict_status, conflict = await asgi_request(
            cast(Any, app),
            method="POST",
            path="/api/v1/evals/experiments",
            body={**body, "metadata": {"changed": True}},
            headers=create_headers,
        )
        invalid_status, invalid = await asgi_request(
            cast(Any, app),
            method="POST",
            path="/api/v1/evals/experiments",
            body=body,
            headers=[
                (b"authorization", b"Bearer invalid"),
                (b"idempotency-key", b"invalid-token-key"),
            ],
        )
        denied_status, denied = await asgi_request(
            cast(Any, app),
            method="POST",
            path="/api/v1/evals/experiments",
            body=body,
            headers=[
                (b"authorization", b"Bearer token-low"),
                (b"idempotency-key", b"low-privilege-key"),
            ],
        )
        missing_key_status, missing_key = await asgi_request(
            cast(Any, app),
            method="POST",
            path="/api/v1/evals/experiments",
            body=body,
            headers=[(b"authorization", b"Bearer token-a")],
        )
        secret_status, secret = await asgi_request(
            cast(Any, app),
            method="POST",
            path=f"/api/v1/evals/experiments/{experiment_id}/accept",
            body={
                "decision": "rejected",
                "reason": "api_key=acceptance-secret-123456",
            },
            headers=[(b"authorization", b"Bearer token-a")],
        )
        metadata_secret_status, metadata_secret = await asgi_request(
            cast(Any, app),
            method="POST",
            path="/api/v1/evals/experiments",
            body={**body, "metadata": {"api_key": "sk-proj-secret-123456789"}},
            headers=[
                (b"authorization", b"Bearer token-a"),
                (b"idempotency-key", b"metadata-secret-key"),
            ],
        )
    finally:
        await storage.dispose()

    assert created_status == 201
    assert replay_status == 200
    assert replay["experiment_id"] == created["experiment_id"]
    assert created["status"] == "completed_with_degradation"
    assert [item["status"] for item in created["provider_statuses"]] == [
        "degraded",
        "degraded",
    ]
    serialized_created = json.dumps(created)
    assert "provider-secret-123456" not in serialized_created
    assert "provider raw body" not in serialized_created
    assert "/Users/alice" not in serialized_created
    assert len(serialized_created) < 10_000
    assert read_status == comparison_status == 200
    assert read["request_id"] == "req-auth-policy-hitl"
    assert comparison["acceptance_recommendation"] == "accept"
    assert accepted_status == accepted_replay_status == 200
    assert accepted["decision_id"] == accepted_replay["decision_id"]
    assert accepted["reviewer_id"] == "reviewer-a"
    assert accepted["production_binding"] is True
    assert cross_status == 404
    assert cross["error"]["code"] == "eval.experiment.not_found"
    assert conflict_status == 409
    assert conflict["error"]["code"] == "eval.experiment.idempotency_conflict"
    assert invalid_status == 401
    assert invalid["error"]["code"] == "auth.invalid_token"
    assert denied_status == 403
    assert denied["error"]["code"] == "policy.denied"
    assert missing_key_status == 422
    assert missing_key["error"]["code"] == "validation_error"
    assert secret_status == 422
    assert secret["error"]["code"] == "validation_error"
    assert metadata_secret_status == 422
    assert metadata_secret["error"]["code"] == "validation_error"
    assert "sk-proj-secret-123456789" not in json.dumps(metadata_secret)
    assert len(evaluator.calls) == 2
    assert table_count(db_path, "eval_experiments") == 1
    assert table_count(db_path, "harness_acceptance_records") == 1
    assert table_count(db_path, "audit_logs") == 1
    assert "baseline" not in json.dumps(accepted)
