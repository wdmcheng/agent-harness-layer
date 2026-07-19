"""评测实验 repository、租户隔离与执行 claim 合同测试。"""

from __future__ import annotations

from tests.contracts.test_eval_experiment_storage_contracts import (
    UTC as UTC,
)
from tests.contracts.test_eval_experiment_storage_contracts import (
    Path as Path,
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
async def test_eval_experiment_repositories_are_tenant_scoped_and_idempotent(
    tmp_path: Path,
) -> None:
    """验证 split 与实验记录同时具备租户隐身、请求重放与内容冲突保护。"""

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
    """验证 execution claim fencing，失去确定执行结果后必须进入人工复核而非继续写终态。"""

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
