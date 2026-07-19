"""人工 experiment acceptance、policy、幂等与原子 audit 合同。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import pytest
from pydantic import ValidationError
from tests.contracts.auth_policy_hitl_contract_helpers import sqlite_dsn, table_count

from agent_harness.identity import IdentityContext


class NeverEvaluator:
    """接受决策路径不应再次运行评估器；任何调用都表示服务边界被绕过。"""

    async def evaluate(self, **_kwargs: Any) -> Any:
        """直接失败以证明接受服务只读取已完成实验和比较结果。"""

        raise AssertionError("acceptance read path must not rerun evaluator")


def _actor(user_id: str = "reviewer-1", *, allowed: bool = True) -> IdentityContext:
    """构造具备或缺少实验接受权限的评审身份夹具。"""

    return IdentityContext(
        tenant_id="tenant-a",
        user_id=user_id,
        session_id=f"session-{user_id}",
        roles=["developer"],
        permissions=["eval.harness.accept"] if allowed else [],
    )


def _manifest(seed: str):
    """构造完整可验证的 harness 版本清单，供接受记录冻结 candidate 身份。"""

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


async def _seed_experiment(
    storage: Any,
    *,
    recommendation: Literal["accept", "reject"] = "accept",
) -> tuple[str, str]:
    """持久化一个已完成实验与比较结果，返回实验和 candidate 版本标识。

    夹具直接写入执行结果而不调用真实评估器，使接受测试聚焦决策、策略、审计和原子
    性，而非数据集切分或模型调用。
    """

    from agent_harness.evals import ExperimentComparison, PerTagComparison
    from agent_harness.evals.experiment_models import RecommendationReasonCode
    from agent_harness.storage import EvalDatasetSplitCreate, EvalExperimentCreate

    baseline = _manifest("baseline")
    candidate = _manifest("candidate")
    async with storage.uow() as uow:
        await uow.tenants.ensure("tenant-a")
        await uow.eval_dataset_splits.create(
            EvalDatasetSplitCreate(
                split_id="split-acceptance",
                tenant_id="tenant-a",
                agent_id="examples.basic",
                dataset="default",
                request_id="split-request",
                tags=["tool_selection"],
                strategy="deterministic_multilabel_v1",
                optimization_ratio=0.8,
                holdout_ratio=0.2,
                case_tags={
                    "opt": ["tool_selection"],
                    "hold": ["tool_selection"],
                },
                optimization_case_ids=["opt"],
                holdout_case_ids=["hold"],
                regression_case_ids=[],
            )
        )
        experiment = await uow.eval_experiments.create(
            EvalExperimentCreate(
                tenant_id="tenant-a",
                idempotency_key=f"acceptance-{recommendation}",
                request_hash=("a" if recommendation == "accept" else "b") * 64,
                request_id="experiment-request",
                agent_id="examples.basic",
                dataset="default",
                split_id="split-acceptance",
                evaluator_profile={"name": "exact-match", "version": "1"},
                metric_versions={"exact_match": "1"},
                baseline_harness=baseline.to_payload(),
                candidate_harness=candidate.to_payload(),
            )
        )
        # 两种推荐分别覆盖接受绑定和拒绝后续处理的决策分支。
        reason_codes: list[RecommendationReasonCode] = (
            [
                "target_tag_improved",
                "holdout_within_threshold",
                "critical_regression_passed",
            ]
            if recommendation == "accept"
            else ["holdout_regression_exceeded", "critical_regression_failed"]
        )
        comparison = ExperimentComparison(
            experiment_id=experiment.experiment_id,
            candidate_harness_version=candidate.version_id,
            per_tag=[
                PerTagComparison(
                    tag="tool_selection",
                    baseline_score=0.5,
                    candidate_score=0.8,
                    delta=0.3,
                )
            ],
            holdout_delta=0.0 if recommendation == "accept" else -0.5,
            regressions=[],
            new_failures=[],
            fixed_failures=[],
            acceptance_recommendation=recommendation,
            recommendation_reason_codes=reason_codes,
            local_evidence_refs=["artifact://comparison/acceptance"],
        )
        claimed = await uow.eval_experiments.claim_execution(
            tenant_id="tenant-a",
            experiment_id=experiment.experiment_id,
            claim_id="acceptance-fixture-claim",
            expires_at=datetime.now(tz=UTC) + timedelta(seconds=30),
        )
        assert claimed is True
        await uow.eval_experiments.update_results(
            tenant_id="tenant-a",
            experiment_id=experiment.experiment_id,
            status="completed",
            baseline_run_ref="eval-run://baseline",
            candidate_run_ref="eval-run://candidate",
            score_summaries={},
            comparison=comparison.to_payload(),
            local_refs=["artifact://comparison/acceptance"],
            provider_statuses=[],
            execution_claim_id="acceptance-fixture-claim",
        )
        await uow.commit()
    return experiment.experiment_id, candidate.version_id


def _acceptance_service(storage: Any, policy: Any):
    """用禁止评估的实验服务装配接受服务，确保测试不意外产生二次执行。"""

    from agent_harness.evals import AcceptanceService, ExperimentService

    experiments = ExperimentService(storage=storage, evaluator=NeverEvaluator())
    return AcceptanceService(storage=storage, experiments=experiments, policy=policy)


def test_acceptance_request_rejects_blank_secret_and_unsafe_refs() -> None:
    """接受请求在入口拒绝空白/敏感理由和不安全的后续引用。"""

    from agent_harness.evals import ExperimentAcceptanceRequest

    for reason in ("   ", "api_key=acceptance-secret-12345"):
        with pytest.raises(ValidationError):
            ExperimentAcceptanceRequest(
                request_id="request",
                decision="rejected",
                reason=reason,
            )
    for ref in ("/Users/alice/private.txt", "file:///tmp/private", "token=secret12345"):
        with pytest.raises(ValidationError):
            ExperimentAcceptanceRequest(
                request_id="request",
                decision="rejected",
                reason="safe rejection reason",
                followup_issue_ref=ref,
            )


@pytest.mark.asyncio
async def test_acceptance_is_atomic_idempotent_and_reviewer_bound(tmp_path: Path) -> None:
    """相同决策幂等重放为一条决定与审计记录，并绑定最初评审者。"""

    from agent_harness.evals import EvalExperimentError, ExperimentAcceptanceRequest
    from agent_harness.policy import PolicyEngine, YamlPolicyProvider
    from agent_harness.storage import SQLAlchemyStorage, run_migrations

    db_path = tmp_path / "acceptance.db"
    dsn = sqlite_dsn(db_path)
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    try:
        experiment_id, candidate_version = await _seed_experiment(storage)
        service = _acceptance_service(storage, PolicyEngine(provider=YamlPolicyProvider.default()))
        request = ExperimentAcceptanceRequest(
            request_id="accept-request",
            decision="accepted",
            reason="all experiment evidence reviewed",
            accepted_harness_version=candidate_version,
        )
        # 第二次调用只改变请求标识，验证重放不创建新的审计副作用。
        accepted = await service.decide(
            actor=_actor(), experiment_id=experiment_id, request=request
        )
        replay = await service.decide(
            actor=_actor(),
            experiment_id=experiment_id,
            request=request.model_copy(update={"request_id": "accept-retry"}),
        )

        assert accepted.decision_id == replay.decision_id
        assert replay.request_id == "accept-retry"
        assert accepted.production_binding is True
        assert accepted.reviewer_id == "reviewer-1"
        assert table_count(db_path, "harness_acceptance_records") == 1
        assert table_count(db_path, "audit_logs") == 1
        with sqlite3.connect(db_path) as connection:
            audit_payload_raw = connection.execute("select payload_json from audit_logs").fetchone()
        assert audit_payload_raw is not None
        audit_payload = json.loads(audit_payload_raw[0])
        assert audit_payload["reviewer_id"] == "reviewer-1"
        assert audit_payload["reason"] == "all experiment evidence reviewed"
        assert audit_payload["candidate_harness_version"] == candidate_version
        assert audit_payload["evidence_refs"] == ["artifact://comparison/acceptance"]

        with pytest.raises(EvalExperimentError) as conflict:
            await service.decide(
                actor=_actor("reviewer-2"),
                experiment_id=experiment_id,
                request=request,
            )
        assert conflict.value.code == "eval.experiment.decision_conflict"
        assert table_count(db_path, "audit_logs") == 1
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_concurrent_identical_accept_reconciles_to_one_decision(tmp_path: Path) -> None:
    """并发相同接受请求必须收敛到一条决定与一条审计记录。"""

    from agent_harness.evals import ExperimentAcceptanceRequest
    from agent_harness.policy import PolicyEngine, YamlPolicyProvider
    from agent_harness.storage import SQLAlchemyStorage, run_migrations

    db_path = tmp_path / "acceptance-concurrent.db"
    dsn = sqlite_dsn(db_path)
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    try:
        experiment_id, candidate_version = await _seed_experiment(storage)
        service = _acceptance_service(storage, PolicyEngine(provider=YamlPolicyProvider.default()))
        request = ExperimentAcceptanceRequest(
            request_id="concurrent-request",
            decision="accepted",
            reason="same concurrent review",
            accepted_harness_version=candidate_version,
        )
        # 同一 storage 并发请求覆盖唯一键竞争与服务层重读协调。
        first, second = await asyncio.gather(
            service.decide(actor=_actor(), experiment_id=experiment_id, request=request),
            service.decide(
                actor=_actor(),
                experiment_id=experiment_id,
                request=request.model_copy(update={"request_id": "concurrent-retry"}),
            ),
        )
        assert first.decision_id == second.decision_id
        assert table_count(db_path, "harness_acceptance_records") == 1
        assert table_count(db_path, "audit_logs") == 1
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_acceptance_version_policy_and_gate_fail_without_decision(tmp_path: Path) -> None:
    """版本不匹配、策略拒绝或要求额外审批时都不得留下任何接受或审计记录。"""

    from agent_harness.evals import EvalExperimentError, ExperimentAcceptanceRequest
    from agent_harness.policy import PolicyEngine, YamlPolicyProvider
    from agent_harness.storage import SQLAlchemyStorage, run_migrations

    db_path = tmp_path / "acceptance-gates.db"
    dsn = sqlite_dsn(db_path)
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    try:
        experiment_id, _candidate = await _seed_experiment(storage)
        request = ExperimentAcceptanceRequest(
            request_id="accept-request",
            decision="accepted",
            reason="reviewed",
            accepted_harness_version="wrong-version",
        )
        allow = _acceptance_service(storage, PolicyEngine(provider=YamlPolicyProvider.default()))
        with pytest.raises(EvalExperimentError) as mismatch:
            await allow.decide(actor=_actor(), experiment_id=experiment_id, request=request)
        assert mismatch.value.code == "eval.experiment.accepted_version_mismatch"

        valid_version = (
            await allow.experiments.compare(
                tenant_id="tenant-a", experiment_id=experiment_id, request_id="compare"
            )
        ).candidate_harness_version
        valid_request = request.model_copy(update={"accepted_harness_version": valid_version})
        deny = _acceptance_service(
            storage, PolicyEngine(provider=YamlPolicyProvider(deny_actions={"eval.harness.accept"}))
        )
        with pytest.raises(EvalExperimentError) as denied:
            await deny.decide(actor=_actor(), experiment_id=experiment_id, request=valid_request)
        assert denied.value.code == "eval.experiment.policy_denied"

        require = _acceptance_service(
            storage,
            PolicyEngine(
                provider=YamlPolicyProvider(require_approval_actions={"eval.harness.accept"})
            ),
        )
        with pytest.raises(EvalExperimentError) as approval:
            await require.decide(actor=_actor(), experiment_id=experiment_id, request=valid_request)
        assert approval.value.code == "eval.experiment.approval_required"
        assert table_count(db_path, "harness_acceptance_records") == 0
        assert table_count(db_path, "audit_logs") == 0
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_rejected_decision_records_audit_without_production_binding(tmp_path: Path) -> None:
    """拒绝决定仍需审计，但不能写入将 candidate 绑定到生产的字段。"""

    from agent_harness.evals import ExperimentAcceptanceRequest
    from agent_harness.policy import PolicyEngine, YamlPolicyProvider
    from agent_harness.storage import SQLAlchemyStorage, run_migrations

    db_path = tmp_path / "acceptance-rejected.db"
    dsn = sqlite_dsn(db_path)
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    try:
        experiment_id, _candidate = await _seed_experiment(storage, recommendation="reject")
        result = await _acceptance_service(
            storage, PolicyEngine(provider=YamlPolicyProvider.default())
        ).decide(
            actor=_actor(),
            experiment_id=experiment_id,
            request=ExperimentAcceptanceRequest(
                request_id="reject-request",
                decision="rejected",
                reason="holdout regression requires follow-up",
                followup_issue_ref="issue://eval/123",
            ),
        )
        assert result.decision == "rejected"
        assert result.production_binding is False
        assert result.accepted_harness_version is None
        with sqlite3.connect(db_path) as connection:
            binding = connection.execute(
                "select production_binding_json from harness_acceptance_records"
            ).fetchone()
        assert binding == (None,)
        assert table_count(db_path, "audit_logs") == 1
    finally:
        await storage.dispose()
