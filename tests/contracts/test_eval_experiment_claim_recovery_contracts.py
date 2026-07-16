"""评测实验 claim 冲突、重启与取消恢复合同测试。"""

from __future__ import annotations

from tests.contracts.test_eval_experiment_recovery_contracts import (
    Any as Any,
)
from tests.contracts.test_eval_experiment_recovery_contracts import (
    BlockingEvaluator as BlockingEvaluator,
)
from tests.contracts.test_eval_experiment_recovery_contracts import (
    Path as Path,
)
from tests.contracts.test_eval_experiment_recovery_contracts import (
    SplitAwareEvaluator as SplitAwareEvaluator,
)
from tests.contracts.test_eval_experiment_recovery_contracts import (
    asyncio as asyncio,
)
from tests.contracts.test_eval_experiment_recovery_contracts import (
    cast as cast,
)
from tests.contracts.test_eval_experiment_recovery_contracts import (
    experiment_request as experiment_request,
)
from tests.contracts.test_eval_experiment_recovery_contracts import (
    pytest as pytest,
)
from tests.contracts.test_eval_experiment_recovery_contracts import (
    seed_approved_cases as seed_approved_cases,
)
from tests.contracts.test_eval_experiment_recovery_contracts import (
    sqlite_dsn as sqlite_dsn,
)
from tests.contracts.test_eval_experiment_recovery_contracts import (
    table_count as table_count,
)


@pytest.mark.asyncio
async def test_overlapping_conflicting_create_rolls_back_loser_split(tmp_path: Path) -> None:
    from agent_harness.evals import EvalExperimentError, ExperimentService
    from agent_harness.storage import SQLAlchemyStorage, run_migrations

    db_path = tmp_path / "conflicting-create.db"
    dsn = sqlite_dsn(db_path)
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    await seed_approved_cases(storage)
    first_request = experiment_request(key="same-key", tags=["tool_selection"])
    conflicting_request = experiment_request(key="same-key", tags=["retrieval_quality"])
    evaluator = BlockingEvaluator(
        first_request.baseline_harness_version.version_id,
        cast(Any, first_request.candidate_harness_version).version_id,
    )
    service = ExperimentService(storage=storage, evaluator=evaluator)
    first_task = asyncio.create_task(service.create(first_request))
    try:
        await evaluator.started.wait()
        with pytest.raises(EvalExperimentError) as conflict:
            await service.create(conflicting_request)
        evaluator.release.set()
        first = await first_task
        async with storage.uow() as uow:
            stored = await uow.eval_experiments.get("tenant-a", first.result.experiment_id)
    finally:
        evaluator.release.set()
        if not first_task.done():
            await first_task
        await storage.dispose()

    assert conflict.value.code == "eval.experiment.idempotency_conflict"
    assert first.created is True
    assert stored is not None
    assert stored.status == "completed"
    assert table_count(db_path, "eval_dataset_splits") == 1
    assert table_count(db_path, "eval_experiments") == 1
    assert len(evaluator.calls) == 2


@pytest.mark.asyncio
async def test_cancelled_worker_becomes_needs_review_without_automatic_replay(
    tmp_path: Path,
) -> None:
    from agent_harness.evals import ExperimentService
    from agent_harness.storage import SQLAlchemyStorage, run_migrations

    db_path = tmp_path / "cancelled-worker.db"
    dsn = sqlite_dsn(db_path)
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    await seed_approved_cases(storage)
    request = experiment_request(key="restart-key")
    candidate = cast(Any, request.candidate_harness_version)
    blocking = BlockingEvaluator(
        request.baseline_harness_version.version_id,
        candidate.version_id,
    )
    first_service = ExperimentService(storage=storage, evaluator=blocking)
    task = asyncio.create_task(first_service.create(request))
    try:
        await blocking.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        async with storage.uow() as uow:
            interrupted = await uow.eval_experiments.get_by_idempotency_key(
                "tenant-a", "restart-key"
            )
        assert interrupted is not None
        assert interrupted.status == "needs_review"
        assert interrupted.execution_claim_id is None

        resumed_evaluator = SplitAwareEvaluator(
            request.baseline_harness_version.version_id,
            candidate.version_id,
        )
        replay = await ExperimentService(
            storage=storage,
            evaluator=resumed_evaluator,
        ).create(request.model_copy(update={"request_id": "request-after-restart"}))
    finally:
        blocking.release.set()
        await storage.dispose()

    assert replay.created is False
    assert replay.result.status == "needs_review"
    assert len(blocking.calls) == 0
    assert len(resumed_evaluator.calls) == 0
    assert table_count(db_path, "eval_dataset_splits") == 1
    assert table_count(db_path, "eval_experiments") == 1


@pytest.mark.asyncio
async def test_expired_running_claim_becomes_needs_review_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_harness.evals import ExperimentService
    from agent_harness.storage import SQLAlchemyStorage, run_migrations

    db_path = tmp_path / "hard-exit.db"
    dsn = sqlite_dsn(db_path)
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    await seed_approved_cases(storage)
    request = experiment_request(key="hard-exit-key")
    candidate = cast(Any, request.candidate_harness_version)
    first_evaluator = SplitAwareEvaluator(
        request.baseline_harness_version.version_id,
        candidate.version_id,
    )
    service = ExperimentService(
        storage=storage,
        evaluator=first_evaluator,
        execution_claim_ttl_seconds=1.0,
    )

    async def simulate_hard_exit(**_kwargs: Any):
        raise SystemExit("simulated process exit after claim commit")

    monkeypatch.setattr(cast(Any, service.execution), "_execute_claimed", simulate_hard_exit)
    try:
        with pytest.raises(SystemExit, match="simulated process exit"):
            await service.create(request)
        await asyncio.sleep(1.1)
        replay_evaluator = SplitAwareEvaluator(
            request.baseline_harness_version.version_id,
            candidate.version_id,
        )
        replay = await ExperimentService(
            storage=storage,
            evaluator=replay_evaluator,
        ).create(request.model_copy(update={"request_id": "after-hard-exit"}))
    finally:
        await storage.dispose()

    assert replay.created is False
    assert replay.result.status == "needs_review"
    assert first_evaluator.calls == []
    assert replay_evaluator.calls == []
    assert table_count(db_path, "eval_dataset_splits") == 1
    assert table_count(db_path, "eval_experiments") == 1


@pytest.mark.asyncio
async def test_legacy_created_replay_becomes_needs_review_without_evaluator(
    tmp_path: Path,
) -> None:
    from agent_harness.evals import ExperimentRequest, ExperimentService
    from agent_harness.storage import (
        EvalDatasetSplitCreate,
        EvalExperimentCreate,
        SQLAlchemyStorage,
        run_migrations,
    )

    db_path = tmp_path / "legacy-created.db"
    dsn = sqlite_dsn(db_path)
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    create_request = experiment_request(key="legacy-created-key")
    candidate = cast(Any, create_request.candidate_harness_version)
    evaluator = SplitAwareEvaluator(
        create_request.baseline_harness_version.version_id,
        candidate.version_id,
    )
    internal_request = ExperimentRequest(
        request_id="legacy-replay",
        tenant_id="tenant-a",
        idempotency_key="legacy-created-key",
        agent_id="examples.basic",
        dataset="default",
        split_id="legacy-split",
        baseline_harness_version=create_request.baseline_harness_version,
        candidate_harness_version=candidate,
        evaluator_profile=create_request.evaluator_profile,
        metric_versions=create_request.metric_versions,
    )
    try:
        async with storage.uow() as uow:
            await uow.tenants.ensure("tenant-a")
            await uow.eval_dataset_splits.create(
                EvalDatasetSplitCreate(
                    split_id="legacy-split",
                    tenant_id="tenant-a",
                    agent_id="examples.basic",
                    dataset="default",
                    request_id="legacy-create",
                    tags=["tool_selection"],
                    strategy="deterministic_multilabel_v1",
                    optimization_ratio=0.8,
                    holdout_ratio=0.2,
                    case_tags={
                        "case-o": ["tool_selection"],
                        "case-h": ["tool_selection"],
                    },
                    optimization_case_ids=["case-o"],
                    holdout_case_ids=["case-h"],
                    regression_case_ids=[],
                )
            )
            legacy = await uow.eval_experiments.create(
                EvalExperimentCreate(
                    tenant_id="tenant-a",
                    idempotency_key="legacy-created-key",
                    request_hash="a" * 64,
                    request_id="legacy-create",
                    agent_id="examples.basic",
                    dataset="default",
                    split_id="legacy-split",
                    evaluator_profile=create_request.evaluator_profile,
                    metric_versions=create_request.metric_versions,
                    baseline_harness=create_request.baseline_harness_version.to_payload(),
                    candidate_harness=candidate.to_payload(),
                )
            )
            await uow.commit()

        outcome = await ExperimentService(
            storage=storage,
            evaluator=evaluator,
        ).execution.resume_or_replay(request=internal_request, record=legacy)
    finally:
        await storage.dispose()

    assert outcome.created is False
    assert outcome.result.status == "needs_review"
    assert evaluator.calls == []
