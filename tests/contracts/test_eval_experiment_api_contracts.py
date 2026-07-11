"""EVL-004 HTTP、OpenAPI、tenant、幂等与 side-effect 合同。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Literal, cast

import pytest
from tests.contracts.auth_policy_hitl_contract_helpers import (
    asgi_request,
    sqlite_dsn,
    table_count,
)

from agent_harness.events import LocalJsonlEventSink
from agent_harness.identity import IdentityContext
from agent_harness.runtime import RunOrchestrator
from app.main import create_app


def experiment_manifest(seed: str):
    from agent_harness.evals import HarnessInputSource, HarnessVersionBuilder

    return HarnessVersionBuilder().build(
        {
            "prompt_instruction": HarnessInputSource(value={"prompt": seed}),
            "tool_descriptions": HarnessInputSource(value=[]),
            "agent_config": HarnessInputSource(value={"max_steps": 4}),
            "retrieval_config": HarnessInputSource(value={"top_k": 5}),
            "policy_defaults": HarnessInputSource(value={"network": "deny"}),
            "model_adapter_settings": HarnessInputSource(value={"adapter": "fake"}),
        }
    )


class SplitAwareEvaluator:
    def __init__(self, baseline_id: str, candidate_id: str) -> None:
        self.baseline_id = baseline_id
        self.candidate_id = candidate_id
        self.calls: list[str] = []

    async def evaluate(self, **kwargs: Any):
        from agent_harness.evals import ExperimentCaseResult, ExperimentEvaluationResult

        split = kwargs["split"]
        version_id = kwargs["harness_version"].version_id
        self.calls.append(version_id)
        cases: list[ExperimentCaseResult] = []
        subsets: dict[str, Literal["optimization", "holdout", "regression"]] = {
            **{case_id: "optimization" for case_id in split.optimization_case_ids},
            **{case_id: "holdout" for case_id in split.holdout_case_ids},
            **{case_id: "regression" for case_id in split.regression_case_ids},
        }
        for case_id, subset in subsets.items():
            score = 0.8
            if subset == "optimization":
                score = 0.5 if version_id == self.baseline_id else 0.9
            cases.append(
                ExperimentCaseResult(
                    case_id=case_id,
                    subset=subset,
                    tags=split.case_tags[case_id],
                    metric_scores={"exact_match": score},
                    passed=True,
                    evidence_refs=[f"artifact://experiment/{version_id}/{case_id}"],
                )
            )
        return ExperimentEvaluationResult(
            harness_version_id=version_id,
            evaluator_profile=kwargs["evaluator_profile"],
            metric_versions=kwargs["metric_versions"],
            case_results=cases,
            local_evidence_refs=[f"artifact://experiment/{version_id}"],
        )


class FailingPublisher:
    provider_name = "optional-eval-provider"

    async def publish(self, payload: dict[str, Any]) -> dict[str, object]:
        del payload
        raise RuntimeError("Authorization: Bearer provider-secret-123456")


class UnsafePublisher:
    provider_name = "unsafe-provider"

    async def publish(self, payload: dict[str, Any]) -> dict[str, object]:
        del payload
        return {
            "provider": "spoofed-provider",
            "status": "completed",
            "detail": "x" * 20_000,
            "raw_response": "provider raw body",
            "evidence_refs": ["/Users/alice/private-provider.json"],
        }


class BlockingEvaluator(SplitAwareEvaluator):
    def __init__(self, baseline_id: str, candidate_id: str) -> None:
        super().__init__(baseline_id, candidate_id)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def evaluate(self, **kwargs: Any):
        if not self.calls:
            self.started.set()
            await self.release.wait()
        return await super().evaluate(**kwargs)


class ExplodingExperimentService:
    def __init__(self) -> None:
        self.calls = 0

    async def create(self, _request: object) -> object:
        self.calls += 1
        raise RuntimeError("Authorization: Bearer internal-provider-secret")


async def seed_approved_cases(storage: Any) -> None:
    from agent_harness.storage import EvalCaseCreate

    async with storage.uow() as uow:
        await uow.tenants.ensure("tenant-a")
        for index in range(3):
            case = await uow.eval_cases.create(
                EvalCaseCreate(
                    tenant_id="tenant-a",
                    agent_id="examples.basic",
                    name=f"case-{index}",
                    payload={"output": {"answer": index}, "expected": {"answer": index}},
                    metadata={"behavior_tags": ["tool_selection", "retrieval_quality"]},
                )
            )
            await uow.eval_cases.approve(
                case_id=case.case_id,
                tenant_id="tenant-a",
                approved_by="curator",
                reason="safe tagged fixture",
            )
        await uow.commit()


def experiment_body() -> tuple[dict[str, object], str]:
    baseline = experiment_manifest("baseline")
    candidate = experiment_manifest("candidate")
    return (
        {
            "agent_id": "examples.basic",
            "dataset": "default",
            "tags": ["tool_selection"],
            "split_strategy": "deterministic_multilabel_v1",
            "baseline_harness_version": baseline.to_payload(),
            "candidate_harness_version": candidate.to_payload(),
        },
        candidate.version_id,
    )


def test_public_create_body_normalizes_tag_order_and_duplicates() -> None:
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
