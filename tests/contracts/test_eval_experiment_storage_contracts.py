"""Eval experiment migration 与 repository 合同测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from tests.contracts.auth_policy_hitl_contract_helpers import sqlite_dsn

from agent_harness.storage import (
    EvalDatasetSplitCreate,
    EvalExperimentCreate,
    HarnessAcceptanceCreate,
)


def split_create() -> EvalDatasetSplitCreate:
    return EvalDatasetSplitCreate(
        split_id="split-1",
        tenant_id="tenant-a",
        agent_id="examples.basic",
        dataset="default",
        request_id="request-1",
        tags=["retrieval_quality", "tool_selection"],
        strategy="deterministic_multilabel_v1",
        optimization_ratio=0.8,
        holdout_ratio=0.2,
        regression_policy={"case_ids": ["case-r"]},
        case_tags={
            "case-o": ["retrieval_quality"],
            "case-h": ["retrieval_quality"],
            "case-r": ["policy_approval"],
        },
        optimization_case_ids=["case-o"],
        holdout_case_ids=["case-h"],
        regression_case_ids=["case-r"],
        tag_distribution={"retrieval_quality": {"optimization": 1, "holdout": 1}},
        rejected_counts={"draft": 2},
        evidence_refs=["artifact://split-1"],
    )


def experiment_create(*, request_hash: str = "a" * 64) -> EvalExperimentCreate:
    return EvalExperimentCreate(
        tenant_id="tenant-a",
        idempotency_key="experiment-key",
        request_hash=request_hash,
        request_id="request-1",
        agent_id="examples.basic",
        dataset="default",
        split_id="split-1",
        evaluator_profile={"name": "exact-match", "version": "1"},
        metric_versions={"exact_match": "1"},
        baseline_harness={"version": "baseline-v1"},
        candidate_harness={"version": "candidate-v2"},
    )


def acceptance_create(*, request_hash: str = "b" * 64) -> HarnessAcceptanceCreate:
    return HarnessAcceptanceCreate(
        tenant_id="tenant-a",
        experiment_id="experiment-1",
        decision_request_hash=request_hash,
        reviewer_id="reviewer-1",
        reason="holdout and regression evidence reviewed",
        decision="accepted",
        accepted_harness_version="candidate-v2",
        production_binding={"agent_id": "examples.basic", "version": "candidate-v2"},
        policy_decision={"decision": "allow", "reason": "manual review allowed"},
        audit_ref="audit://acceptance-1",
        evidence_refs=["artifact://comparison-1"],
    )


@pytest.mark.asyncio
async def test_phase_12_5_repositories_are_tenant_scoped_and_idempotent(
    tmp_path: Path,
) -> None:
    from agent_harness.storage import (
        ExperimentStorageConflict,
        ExperimentStorageNotFound,
        SQLAlchemyStorage,
        run_migrations,
    )

    dsn = sqlite_dsn(tmp_path / "eval-experiment-repository.db")
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    try:
        async with storage.uow() as uow:
            await uow.tenants.ensure("tenant-a")
            await uow.tenants.ensure("tenant-b")
            split = await uow.eval_dataset_splits.create(split_create())
            split_replay = await uow.eval_dataset_splits.create(
                split_create().model_copy(update={"request_id": "request-retry"})
            )
            experiment = await uow.eval_experiments.create(experiment_create())
            replay = await uow.eval_experiments.create(
                experiment_create().model_copy(update={"request_id": "request-retry"})
            )
            await uow.commit()

        assert split.split_id == "split-1"
        assert split_replay.split_id == split.split_id
        assert experiment.experiment_id == replay.experiment_id
        assert experiment.request_hash == "a" * 64

        async with storage.uow() as uow:
            assert await uow.eval_dataset_splits.get("tenant-b", "split-1") is None
            assert await uow.eval_experiments.get("tenant-b", experiment.experiment_id) is None
            with pytest.raises(ExperimentStorageConflict) as conflict:
                await uow.eval_experiments.create(experiment_create(request_hash="c" * 64))
        assert conflict.value.code == "eval.experiment.idempotency_conflict"

        async with storage.uow() as uow:
            cross_tenant = experiment_create().model_copy(
                update={"tenant_id": "tenant-b", "idempotency_key": "tenant-b-key"}
            )
            with pytest.raises(ExperimentStorageNotFound) as hidden_split:
                await uow.eval_experiments.create(cross_tenant)
        assert hidden_split.value.code == "eval.experiment.split_not_found"

        async with storage.uow() as uow:
            wrong_agent = experiment_create().model_copy(
                update={"agent_id": "examples.other", "idempotency_key": "wrong-agent-key"}
            )
            with pytest.raises(ExperimentStorageNotFound):
                await uow.eval_experiments.create(wrong_agent)

        async with storage.uow() as uow:
            changed_body = experiment_create().model_copy(
                update={"candidate_harness": {"version": "candidate-v3"}}
            )
            with pytest.raises(ExperimentStorageConflict):
                await uow.eval_experiments.create(changed_body)
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_execution_claim_is_fenced_and_uncertain_outcome_needs_review(
    tmp_path: Path,
) -> None:
    from agent_harness.storage import ExperimentStorageConflict, SQLAlchemyStorage, run_migrations

    dsn = sqlite_dsn(tmp_path / "eval-experiment-claim.db")
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    now = datetime.now(tz=UTC)
    try:
        async with storage.uow() as uow:
            await uow.tenants.ensure("tenant-a")
            await uow.eval_dataset_splits.create(split_create())
            experiment = await uow.eval_experiments.create(experiment_create())
            claimed = await uow.eval_experiments.claim_execution(
                tenant_id="tenant-a",
                experiment_id=experiment.experiment_id,
                claim_id="owner-claim",
                expires_at=now + timedelta(seconds=30),
            )
            await uow.commit()
        assert claimed is True

        async with storage.uow() as uow:
            second_claim = await uow.eval_experiments.claim_execution(
                tenant_id="tenant-a",
                experiment_id=experiment.experiment_id,
                claim_id="other-claim",
                expires_at=now + timedelta(seconds=30),
            )
            with pytest.raises(ExperimentStorageConflict) as fenced:
                await uow.eval_experiments.update_results(
                    tenant_id="tenant-a",
                    experiment_id=experiment.experiment_id,
                    status="completed",
                    baseline_run_ref="eval-run://baseline",
                    candidate_run_ref="eval-run://candidate",
                    score_summaries={},
                    comparison={},
                    local_refs=[],
                    provider_statuses=[],
                    execution_claim_id="other-claim",
                )
        assert second_claim is False
        assert fenced.value.code == "eval.experiment.execution_fenced"

        async with storage.uow() as uow:
            renewed = await uow.eval_experiments.renew_execution_claim(
                tenant_id="tenant-a",
                experiment_id=experiment.experiment_id,
                claim_id="owner-claim",
                expires_at=now + timedelta(seconds=60),
            )
            marked = await uow.eval_experiments.mark_execution_needs_review(
                tenant_id="tenant-a",
                experiment_id=experiment.experiment_id,
                claim_id="owner-claim",
                reason_code="eval.experiment.execution_outcome_uncertain",
            )
            await uow.commit()
        assert renewed is True
        assert marked is True

        async with storage.uow() as uow:
            stored = await uow.eval_experiments.get("tenant-a", experiment.experiment_id)
        assert stored is not None
        assert stored.status == "needs_review"
        assert stored.execution_claim_id is None
        assert stored.execution_claim_expires_at is None

        async with storage.uow() as uow:
            with pytest.raises(ExperimentStorageConflict) as stale_after_review:
                await uow.eval_experiments.update_results(
                    tenant_id="tenant-a",
                    experiment_id=experiment.experiment_id,
                    status="completed",
                    baseline_run_ref="eval-run://late-baseline",
                    candidate_run_ref="eval-run://late-candidate",
                    score_summaries={"stale": True},
                    comparison={},
                    local_refs=[],
                    provider_statuses=[],
                    execution_claim_id="owner-claim",
                )
        assert stale_after_review.value.code == "eval.experiment.execution_fenced"

        expired_data = experiment_create(request_hash="e" * 64).model_copy(
            update={"idempotency_key": "expired-claim-key"}
        )
        async with storage.uow() as uow:
            expired = await uow.eval_experiments.create(expired_data)
            await uow.eval_experiments.claim_execution(
                tenant_id="tenant-a",
                experiment_id=expired.experiment_id,
                claim_id="expired-owner",
                expires_at=now - timedelta(seconds=1),
            )
            await uow.commit()
        async with storage.uow() as uow:
            renewed_expired = await uow.eval_experiments.renew_execution_claim(
                tenant_id="tenant-a",
                experiment_id=expired.experiment_id,
                claim_id="expired-owner",
                expires_at=now + timedelta(seconds=60),
            )
            with pytest.raises(ExperimentStorageConflict) as expired_terminal:
                await uow.eval_experiments.update_results(
                    tenant_id="tenant-a",
                    experiment_id=expired.experiment_id,
                    status="completed",
                    baseline_run_ref="eval-run://late-baseline",
                    candidate_run_ref=None,
                    score_summaries={"stale": True},
                    comparison={},
                    local_refs=[],
                    provider_statuses=[],
                    execution_claim_id="expired-owner",
                )
        assert renewed_expired is False
        assert expired_terminal.value.code == "eval.experiment.execution_fenced"

        async with storage.uow() as uow:
            marked_expired = await uow.eval_experiments.mark_expired_execution_needs_review(
                tenant_id="tenant-a",
                experiment_id=expired.experiment_id,
                now=now,
            )
            with pytest.raises(ExperimentStorageConflict) as late_expired:
                await uow.eval_experiments.update_results(
                    tenant_id="tenant-a",
                    experiment_id=expired.experiment_id,
                    status="completed",
                    baseline_run_ref="eval-run://late-baseline",
                    candidate_run_ref=None,
                    score_summaries={"stale": True},
                    comparison={},
                    local_refs=[],
                    provider_statuses=[],
                    execution_claim_id="expired-owner",
                )
            await uow.commit()
        assert marked_expired is True
        assert late_expired.value.code == "eval.experiment.execution_fenced"
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_phase_12_5_result_update_and_acceptance_decision_are_immutable(
    tmp_path: Path,
) -> None:
    from agent_harness.storage import (
        ExperimentStorageConflict,
        ExperimentStorageNotFound,
        SQLAlchemyStorage,
        run_migrations,
    )

    dsn = sqlite_dsn(tmp_path / "eval-experiment-results.db")
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    try:
        async with storage.uow() as uow:
            await uow.tenants.ensure("tenant-a")
            await uow.tenants.ensure("tenant-b")
            await uow.eval_dataset_splits.create(split_create())
            experiment = await uow.eval_experiments.create(experiment_create())
            claimed = await uow.eval_experiments.claim_execution(
                tenant_id="tenant-a",
                experiment_id=experiment.experiment_id,
                claim_id="result-owner",
                expires_at=datetime.now(tz=UTC) + timedelta(seconds=30),
            )
            assert claimed is True
            completed = await uow.eval_experiments.update_results(
                tenant_id="tenant-a",
                experiment_id=experiment.experiment_id,
                status="completed",
                baseline_run_ref="eval-run://baseline",
                candidate_run_ref="eval-run://candidate",
                score_summaries={
                    "baseline": {"exact_match": 0.6},
                    "candidate": {"exact_match": 0.8},
                },
                comparison={"recommendation": "accept"},
                local_refs=["artifact://comparison-1"],
                provider_statuses=[],
                execution_claim_id="result-owner",
            )
            with_provider = await uow.eval_experiments.update_provider_results(
                tenant_id="tenant-a",
                experiment_id=experiment.experiment_id,
                expected_status="completed",
                status="completed_with_degradation",
                comparison={"recommendation": "accept"},
                provider_statuses=[{"provider": "optional", "status": "degraded"}],
            )
            acceptance_data = acceptance_create()
            acceptance_data.experiment_id = experiment.experiment_id
            decision = await uow.harness_acceptance_records.create(acceptance_data)
            replay = await uow.harness_acceptance_records.create(acceptance_data)
            await uow.commit()

        assert completed.status == "completed"
        assert with_provider.status == "completed_with_degradation"
        assert with_provider.provider_statuses[0]["status"] == "degraded"
        assert decision.acceptance_id == replay.acceptance_id

        async with storage.uow() as uow:
            with pytest.raises(ExperimentStorageConflict) as terminal_overwrite:
                await uow.eval_experiments.update_results(
                    tenant_id="tenant-a",
                    experiment_id=experiment.experiment_id,
                    status="failed",
                    baseline_run_ref=None,
                    candidate_run_ref=None,
                    score_summaries={"stale": True},
                    comparison={},
                    local_refs=[],
                    provider_statuses=[],
                    execution_claim_id="result-owner",
                )
            with pytest.raises(ExperimentStorageConflict) as provider_replay:
                await uow.eval_experiments.update_provider_results(
                    tenant_id="tenant-a",
                    experiment_id=experiment.experiment_id,
                    expected_status="completed",
                    status="completed_with_degradation",
                    comparison={},
                    provider_statuses=[],
                )
        assert terminal_overwrite.value.code == "eval.experiment.execution_fenced"
        assert provider_replay.value.code == "eval.experiment.execution_fenced"

        async with storage.uow() as uow:
            replay_with_new_evidence = acceptance_create()
            replay_with_new_evidence.experiment_id = experiment.experiment_id
            replay_with_new_evidence.audit_ref = "audit://retry-is-correlation"
            replay_with_new_evidence.evidence_refs = ["artifact://retry-derived"]
            replayed = await uow.harness_acceptance_records.create(replay_with_new_evidence)
        assert replayed.acceptance_id == decision.acceptance_id

        async with storage.uow() as uow:
            changed = acceptance_create(request_hash="d" * 64)
            changed.experiment_id = experiment.experiment_id
            with pytest.raises(ExperimentStorageConflict) as conflict:
                await uow.harness_acceptance_records.create(changed)
        assert conflict.value.code == "eval.experiment.decision_conflict"

        async with storage.uow() as uow:
            changed_reviewer = acceptance_create()
            changed_reviewer.experiment_id = experiment.experiment_id
            changed_reviewer.reviewer_id = "reviewer-2"
            with pytest.raises(ExperimentStorageConflict):
                await uow.harness_acceptance_records.create(changed_reviewer)
            assert (
                await uow.harness_acceptance_records.get_for_experiment(
                    "tenant-b", experiment.experiment_id
                )
                is None
            )

            cross_tenant = acceptance_create().model_copy(
                update={
                    "tenant_id": "tenant-b",
                    "experiment_id": experiment.experiment_id,
                }
            )
            with pytest.raises(ExperimentStorageNotFound) as hidden_experiment:
                await uow.harness_acceptance_records.create(cross_tenant)
        assert hidden_experiment.value.code == "eval.experiment.not_found"
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_phase_12_5_unit_of_work_rolls_back_without_commit(tmp_path: Path) -> None:
    from agent_harness.storage import SQLAlchemyStorage, run_migrations

    dsn = sqlite_dsn(tmp_path / "eval-experiment-rollback.db")
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    try:
        async with storage.uow() as uow:
            await uow.tenants.ensure("tenant-a")
            await uow.eval_dataset_splits.create(split_create())

        async with storage.uow() as uow:
            assert await uow.eval_dataset_splits.get("tenant-a", "split-1") is None
    finally:
        await storage.dispose()


def test_acceptance_dto_rejects_impossible_production_bindings() -> None:
    with pytest.raises(ValidationError):
        HarnessAcceptanceCreate(
            tenant_id="tenant-a",
            experiment_id="experiment-1",
            decision_request_hash="b" * 64,
            reviewer_id="reviewer-1",
            reason="missing accepted binding",
            decision="accepted",
            policy_decision={"decision": "allow"},
            audit_ref="audit://acceptance-1",
        )

    with pytest.raises(ValidationError):
        HarnessAcceptanceCreate(
            tenant_id="tenant-a",
            experiment_id="experiment-1",
            decision_request_hash="b" * 64,
            reviewer_id="reviewer-1",
            reason="rejected must not bind",
            decision="rejected",
            accepted_harness_version="candidate-v2",
            production_binding={"version": "candidate-v2"},
            policy_decision={"decision": "allow"},
            audit_ref="audit://acceptance-1",
        )
