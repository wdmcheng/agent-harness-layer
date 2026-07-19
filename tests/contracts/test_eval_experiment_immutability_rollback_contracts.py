"""评测实验结果不可变性与事务回滚合同测试。"""

from __future__ import annotations

from tests.contracts.test_eval_experiment_storage_contracts import (
    UTC as UTC,
)
from tests.contracts.test_eval_experiment_storage_contracts import (
    HarnessAcceptanceCreate as HarnessAcceptanceCreate,
)
from tests.contracts.test_eval_experiment_storage_contracts import (
    Path as Path,
)
from tests.contracts.test_eval_experiment_storage_contracts import (
    ValidationError as ValidationError,
)
from tests.contracts.test_eval_experiment_storage_contracts import (
    acceptance_create as acceptance_create,
)
from tests.contracts.test_eval_experiment_storage_contracts import (
    datetime as datetime,
)
from tests.contracts.test_eval_experiment_storage_contracts import (
    experiment_create as experiment_create,
)
from tests.contracts.test_eval_experiment_storage_contracts import (
    pytest as pytest,
)
from tests.contracts.test_eval_experiment_storage_contracts import (
    split_create as split_create,
)
from tests.contracts.test_eval_experiment_storage_contracts import (
    sqlite_dsn as sqlite_dsn,
)
from tests.contracts.test_eval_experiment_storage_contracts import (
    timedelta as timedelta,
)


@pytest.mark.asyncio
async def test_eval_experiment_result_update_and_acceptance_decision_are_immutable(
    tmp_path: Path,
) -> None:
    """验证终态实验结果与接受决定只能重放，不得被新请求覆写或跨租户读取。"""

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
async def test_eval_experiment_unit_of_work_rolls_back_without_commit(tmp_path: Path) -> None:
    """验证未显式提交的评测 split 写入会在 UoW 退出时回滚。"""

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
    """验证接受和拒绝决定各自只能携带语义一致的生产版本绑定。"""

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
