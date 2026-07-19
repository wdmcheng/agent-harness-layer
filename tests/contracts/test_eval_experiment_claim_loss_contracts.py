"""评测实验 claim 丢失与结果写入失败合同测试。"""

from __future__ import annotations

from tests.contracts.test_eval_experiment_recovery_contracts import (
    UTC as UTC,
)
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
    datetime as datetime,
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


@pytest.mark.parametrize("failure_mode", ["return_false", "raise"])
@pytest.mark.asyncio
async def test_heartbeat_claim_loss_prevents_terminal_result_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
) -> None:
    """执行 claim 心跳丢失或报错后，实验必须收敛为需复核，禁止继续写终态评估结果。"""

    from agent_harness.evals import ExperimentService
    from agent_harness.storage import SQLAlchemyStorage, run_migrations
    from agent_harness.storage.eval_experiment_repositories import EvalExperimentRepository

    db_path = tmp_path / "heartbeat-claim-loss.db"
    dsn = sqlite_dsn(db_path)
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    await seed_approved_cases(storage)
    request = experiment_request(key="heartbeat-claim-loss-key")
    candidate = cast(Any, request.candidate_harness_version)
    evaluator = BlockingEvaluator(
        request.baseline_harness_version.version_id,
        candidate.version_id,
    )
    renew_calls = 0
    renew_attempted = asyncio.Event()

    async def lose_claim(self: Any, **_kwargs: Any) -> bool:
        """在首次续租时模拟 claim 丢失或存储异常，保留计数以验证后台心跳确实运行过。"""

        nonlocal renew_calls
        renew_calls += 1
        renew_attempted.set()
        if failure_mode == "raise":
            raise RuntimeError("simulated heartbeat storage failure")
        return False

    monkeypatch.setattr(EvalExperimentRepository, "renew_execution_claim", lose_claim)
    task = asyncio.create_task(
        ExperimentService(
            storage=storage,
            evaluator=evaluator,
            execution_claim_ttl_seconds=3.0,
        ).create(request)
    )
    try:
        await evaluator.started.wait()
        await asyncio.wait_for(renew_attempted.wait(), timeout=2.0)
        async with storage.uow() as uow:
            live = await uow.eval_experiments.get_by_idempotency_key(
                "tenant-a", "heartbeat-claim-loss-key"
            )
        assert live is not None
        assert live.execution_claim_expires_at is not None
        live_expiry = live.execution_claim_expires_at
        if live_expiry.tzinfo is None:
            live_expiry = live_expiry.replace(tzinfo=UTC)
        assert live_expiry > datetime.now(tz=UTC)
        evaluator.release.set()
        outcome = await task
        async with storage.uow() as uow:
            stored = await uow.eval_experiments.get_by_idempotency_key(
                "tenant-a", "heartbeat-claim-loss-key"
            )
    finally:
        evaluator.release.set()
        if not task.done():
            await task
        await storage.dispose()

    assert renew_calls >= 1
    assert outcome.result.status == "needs_review"
    assert stored is not None
    assert stored.status == "needs_review"
    assert stored.execution_claim_id is None
    assert stored.execution_claim_expires_at is None


@pytest.mark.asyncio
async def test_result_write_failure_requires_review_without_repeating_evaluator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """结果落库失败后需关闭 live claim 并转人工复核；同键重放不能再次调用 evaluator。"""

    from agent_harness.evals import ExperimentService
    from agent_harness.storage import SQLAlchemyStorage, run_migrations
    from agent_harness.storage.eval_experiment_repositories import EvalExperimentRepository

    db_path = tmp_path / "result-write-failure.db"
    dsn = sqlite_dsn(db_path)
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    await seed_approved_cases(storage)
    request = experiment_request(key="write-recovery-key")
    candidate = cast(Any, request.candidate_harness_version)
    evaluator = SplitAwareEvaluator(
        request.baseline_harness_version.version_id,
        candidate.version_id,
    )
    original = EvalExperimentRepository.update_results
    calls = 0

    async def fail_once(self: Any, **kwargs: Any):
        """仅阻断第一次结果写入，构造 evaluator 已执行但持久化未完成的恢复边界。"""

        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("simulated result write failure")
        return await original(self, **kwargs)

    monkeypatch.setattr(EvalExperimentRepository, "update_results", fail_once)
    try:
        with pytest.raises(RuntimeError, match="simulated result write failure"):
            await ExperimentService(storage=storage, evaluator=evaluator).create(request)
        async with storage.uow() as uow:
            interrupted = await uow.eval_experiments.get_by_idempotency_key(
                "tenant-a", "write-recovery-key"
            )
        assert interrupted is not None
        assert interrupted.status == "needs_review"
        assert interrupted.execution_claim_id is None

        replay = await ExperimentService(storage=storage, evaluator=evaluator).create(
            request.model_copy(update={"request_id": "write-replay"})
        )
    finally:
        await storage.dispose()

    assert replay.created is False
    assert replay.result.status == "needs_review"
    assert len(evaluator.calls) == 2
    assert table_count(db_path, "eval_dataset_splits") == 1
    assert table_count(db_path, "eval_experiments") == 1
