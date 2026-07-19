"""评测器结果清洗与证据引用边界合同测试。"""

from __future__ import annotations

from tests.contracts.test_eval_experiment_evidence_boundaries_contracts import (
    Any as Any,
)
from tests.contracts.test_eval_experiment_evidence_boundaries_contracts import (
    LargeRawErrorEvaluator as LargeRawErrorEvaluator,
)
from tests.contracts.test_eval_experiment_evidence_boundaries_contracts import (
    Path as Path,
)
from tests.contracts.test_eval_experiment_evidence_boundaries_contracts import (
    UnsafeSuccessfulEvaluator as UnsafeSuccessfulEvaluator,
)
from tests.contracts.test_eval_experiment_evidence_boundaries_contracts import (
    ValidationError as ValidationError,
)
from tests.contracts.test_eval_experiment_evidence_boundaries_contracts import (
    cast as cast,
)
from tests.contracts.test_eval_experiment_evidence_boundaries_contracts import (
    experiment_request as experiment_request,
)
from tests.contracts.test_eval_experiment_evidence_boundaries_contracts import (
    json as json,
)
from tests.contracts.test_eval_experiment_evidence_boundaries_contracts import (
    pytest as pytest,
)
from tests.contracts.test_eval_experiment_evidence_boundaries_contracts import (
    seed_approved_cases as seed_approved_cases,
)
from tests.contracts.test_eval_experiment_evidence_boundaries_contracts import (
    sqlite_dsn as sqlite_dsn,
)


@pytest.mark.asyncio
async def test_evaluator_raw_error_is_replaced_by_bounded_structured_summary(
    tmp_path: Path,
) -> None:
    """验证 evaluator 原始异常会被替换为有界、无敏感信息的结构化失败摘要。"""

    from agent_harness.evals import ExperimentService
    from agent_harness.storage import SQLAlchemyStorage, run_migrations

    db_path = tmp_path / "large-evaluator-error.db"
    dsn = sqlite_dsn(db_path)
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    await seed_approved_cases(storage)
    request = experiment_request(key="large-error-key")
    try:
        outcome = await ExperimentService(
            storage=storage,
            evaluator=LargeRawErrorEvaluator(),
        ).create(request)
        async with storage.uow() as uow:
            stored = await uow.eval_experiments.get("tenant-a", outcome.result.experiment_id)
    finally:
        await storage.dispose()

    assert outcome.result.status == "failed"
    assert stored is not None
    encoded = json.dumps(stored.score_summaries)
    assert "provider raw response" not in encoded
    assert len(encoded) < 500
    assert stored.score_summaries["error"]["code"] == "eval.experiment.evaluation_failed"


@pytest.mark.parametrize(
    "unsafe_mode",
    ["secret", "absolute_path", "oversized", "oversized_list"],
)
@pytest.mark.asyncio
async def test_successful_evaluator_result_rejects_unsafe_or_oversized_evidence(
    tmp_path: Path, unsafe_mode: str
) -> None:
    """验证表面成功但携带不安全证据的 evaluator 结果仍会被失败关闭。"""

    from agent_harness.evals import ExperimentService
    from agent_harness.storage import SQLAlchemyStorage, run_migrations

    db_path = tmp_path / "unsafe-success-result.db"
    dsn = sqlite_dsn(db_path)
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    await seed_approved_cases(storage)
    request = experiment_request(key="unsafe-success-key")
    candidate = cast(Any, request.candidate_harness_version)
    evaluator = UnsafeSuccessfulEvaluator(
        request.baseline_harness_version.version_id,
        candidate.version_id,
        unsafe_mode,
    )
    try:
        outcome = await ExperimentService(storage=storage, evaluator=evaluator).create(request)
        async with storage.uow() as uow:
            stored = await uow.eval_experiments.get("tenant-a", outcome.result.experiment_id)
    finally:
        await storage.dispose()

    assert outcome.result.status == "failed"
    assert stored is not None
    encoded = json.dumps(
        {
            "result": outcome.result.to_payload(),
            "scores": stored.score_summaries,
            "refs": stored.local_refs,
        }
    )
    assert "successful-evaluator-secret" not in encoded
    assert "/Users/alice" not in encoded
    assert len(encoded) < 2_000


@pytest.mark.parametrize(
    "unsafe_ref",
    [
        "api_key=unsafe-dto-secret-123456",
        "/Users/alice/private-evidence.json",
        "x" * 2_049,
    ],
)
def test_evaluator_result_dto_rejects_unsafe_evidence_refs(unsafe_ref: str) -> None:
    """验证 DTO 在入口拒绝秘密、绝对路径与超长 evidence 引用。"""

    from agent_harness.evals import ExperimentCaseResult

    with pytest.raises(ValidationError):
        ExperimentCaseResult(
            case_id="case-1",
            subset="holdout",
            tags=["tool_selection"],
            metric_scores={"exact_match": 1.0},
            passed=True,
            evidence_refs=[unsafe_ref],
        )
