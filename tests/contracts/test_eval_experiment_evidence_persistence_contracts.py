"""评测实验证据边界与持久化失败合同测试。"""

from __future__ import annotations

from tests.contracts.test_eval_experiment_evidence_boundaries_contracts import (
    Any as Any,
)
from tests.contracts.test_eval_experiment_evidence_boundaries_contracts import (
    BoundaryCaseRefsEvaluator as BoundaryCaseRefsEvaluator,
)
from tests.contracts.test_eval_experiment_evidence_boundaries_contracts import (
    BoundaryRefsEvaluator as BoundaryRefsEvaluator,
)
from tests.contracts.test_eval_experiment_evidence_boundaries_contracts import (
    CapturingPublisher as CapturingPublisher,
)
from tests.contracts.test_eval_experiment_evidence_boundaries_contracts import (
    InvalidPartialEvaluator as InvalidPartialEvaluator,
)
from tests.contracts.test_eval_experiment_evidence_boundaries_contracts import (
    Path as Path,
)
from tests.contracts.test_eval_experiment_evidence_boundaries_contracts import (
    SplitAwareEvaluator as SplitAwareEvaluator,
)
from tests.contracts.test_eval_experiment_evidence_boundaries_contracts import (
    cast as cast,
)
from tests.contracts.test_eval_experiment_evidence_boundaries_contracts import (
    experiment_request as experiment_request,
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


@pytest.mark.parametrize(
    ("mode", "baseline_count", "candidate_count", "ref_padding"),
    [
        ("baseline_limit", 100, 0, 0),
        ("combined_count", 60, 60, 0),
        ("combined_bytes", 45, 45, 170),
    ],
)
@pytest.mark.asyncio
async def test_derived_public_refs_are_bounded_before_terminal_persistence(
    tmp_path: Path,
    mode: str,
    baseline_count: int,
    candidate_count: int,
    ref_padding: int,
) -> None:
    from agent_harness.evals import ExperimentService
    from agent_harness.storage import SQLAlchemyStorage, run_migrations

    db_path = tmp_path / f"bounded-derived-refs-{mode}.db"
    dsn = sqlite_dsn(db_path)
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    await seed_approved_cases(storage)
    request = experiment_request(key=f"bounded-derived-refs-{mode}")
    candidate = cast(Any, request.candidate_harness_version)

    def refs(version: str, count: int) -> list[str]:
        return [
            f"artifact://bounded/{version}/{index}/" + "x" * ref_padding for index in range(count)
        ]

    refs_by_version = {
        request.baseline_harness_version.version_id: refs("baseline", baseline_count),
        candidate.version_id: refs("candidate", candidate_count),
    }
    if candidate_count == 0:
        request = request.model_copy(update={"candidate_harness_version": None})
    publisher = CapturingPublisher()
    service = ExperimentService(
        storage=storage,
        evaluator=BoundaryRefsEvaluator(
            request.baseline_harness_version.version_id,
            candidate.version_id,
            refs_by_version,
        ),
        publishers=[publisher],
    )
    try:
        outcome = await service.create(request)
        shown = await service.get(
            tenant_id="tenant-a",
            experiment_id=outcome.result.experiment_id,
            request_id="show-bounded-refs",
        )
        replay = await service.create(
            request.model_copy(update={"request_id": "replay-bounded-refs"})
        )
        comparison = (
            None
            if candidate_count == 0
            else await service.compare(
                tenant_id="tenant-a",
                experiment_id=outcome.result.experiment_id,
                request_id="compare-bounded-refs",
            )
        )
        async with storage.uow() as uow:
            stored = await uow.eval_experiments.get("tenant-a", outcome.result.experiment_id)
    finally:
        await storage.dispose()

    truth_ref = f"db://eval-experiments/{outcome.result.experiment_id}"
    assert stored is not None
    assert stored.status in {"baseline_completed", "completed"}
    assert stored.local_refs == [truth_ref]
    assert outcome.result.local_evidence_refs == [truth_ref]
    assert shown.local_evidence_refs == [truth_ref]
    assert replay.result.local_evidence_refs == [truth_ref]
    assert publisher.payloads[0]["local_evidence_refs"] == [truth_ref]
    if comparison is not None:
        assert comparison.local_evidence_refs == [truth_ref]
        assert stored.comparison["local_evidence_refs"] == [truth_ref]


@pytest.mark.parametrize(
    ("mode", "ref_count", "ref_padding"),
    [("combined_count", 60, 0), ("combined_bytes", 45, 170)],
)
@pytest.mark.asyncio
async def test_failure_difference_refs_are_bounded_before_comparison_persistence(
    tmp_path: Path,
    mode: str,
    ref_count: int,
    ref_padding: int,
) -> None:
    from agent_harness.evals import ExperimentService
    from agent_harness.storage import SQLAlchemyStorage, run_migrations

    dsn = sqlite_dsn(tmp_path / f"bounded-failure-difference-{mode}.db")
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    await seed_approved_cases(storage)
    request = experiment_request(key=f"bounded-failure-difference-{mode}")
    candidate = cast(Any, request.candidate_harness_version)
    publisher = CapturingPublisher()
    service = ExperimentService(
        storage=storage,
        evaluator=BoundaryCaseRefsEvaluator(
            request.baseline_harness_version.version_id,
            candidate.version_id,
            ref_count=ref_count,
            ref_padding=ref_padding,
        ),
        publishers=[publisher],
    )
    try:
        outcome = await service.create(request)
        shown = await service.get(
            tenant_id="tenant-a",
            experiment_id=outcome.result.experiment_id,
            request_id="show-bounded-failure-difference",
        )
        compared = await service.compare(
            tenant_id="tenant-a",
            experiment_id=outcome.result.experiment_id,
            request_id="compare-bounded-failure-difference",
        )
        replay = await service.create(
            request.model_copy(update={"request_id": "replay-bounded-failure-difference"})
        )
        async with storage.uow() as uow:
            stored = await uow.eval_experiments.get("tenant-a", outcome.result.experiment_id)
    finally:
        await storage.dispose()

    truth_ref = f"db://eval-experiments/{outcome.result.experiment_id}"
    assert stored is not None
    assert outcome.result.status == "completed"
    assert shown.status == "completed"
    assert replay.result.status == "completed"
    assert compared.new_failures[0].evidence_refs == [truth_ref]
    assert stored.comparison["new_failures"][0]["evidence_refs"] == [truth_ref]
    provider_comparison = cast(dict[str, Any], publisher.payloads[0]["comparison"])
    assert provider_comparison["new_failures"][0]["evidence_refs"] == [truth_ref]
    assert len(stored.score_summaries["baseline"]["case_results"][0]["evidence_refs"]) == ref_count
    assert len(stored.score_summaries["candidate"]["case_results"][0]["evidence_refs"]) == ref_count


@pytest.mark.asyncio
async def test_create_persists_draft_rejection_without_scoring_draft(tmp_path: Path) -> None:
    from agent_harness.evals import ExperimentService
    from agent_harness.storage import EvalCaseCreate, SQLAlchemyStorage, run_migrations

    dsn = sqlite_dsn(tmp_path / "create-draft-rejection.db")
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    await seed_approved_cases(storage)
    async with storage.uow() as uow:
        draft = await uow.eval_cases.create(
            EvalCaseCreate(
                tenant_id="tenant-a",
                agent_id="examples.basic",
                name="draft-not-scored",
                payload={"output": {"answer": 1}, "expected": {"answer": 1}},
                metadata={"behavior_tags": ["tool_selection"]},
            )
        )
        await uow.commit()
    request = experiment_request(key="create-draft-rejection-key")
    candidate = cast(Any, request.candidate_harness_version)
    evaluator = SplitAwareEvaluator(
        request.baseline_harness_version.version_id,
        candidate.version_id,
    )
    try:
        outcome = await ExperimentService(storage=storage, evaluator=evaluator).create(request)
        async with storage.uow() as uow:
            experiment = await uow.eval_experiments.get("tenant-a", outcome.result.experiment_id)
            assert experiment is not None
            split = await uow.eval_dataset_splits.get("tenant-a", experiment.split_id)
    finally:
        await storage.dispose()

    assert split is not None
    assert split.rejected_counts["not_approved"] == 1
    memberships = {
        *split.optimization_case_ids,
        *split.holdout_case_ids,
        *split.regression_case_ids,
    }
    assert draft.case_id not in memberships
    assert len(evaluator.calls) == 2


@pytest.mark.asyncio
async def test_invalid_partial_result_becomes_terminal_failure_without_live_claim(
    tmp_path: Path,
) -> None:
    from agent_harness.evals import ExperimentService
    from agent_harness.storage import SQLAlchemyStorage, run_migrations

    db_path = tmp_path / "invalid-partial.db"
    dsn = sqlite_dsn(db_path)
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    await seed_approved_cases(storage)
    request = experiment_request(key="invalid-partial-key")
    try:
        service = ExperimentService(storage=storage, evaluator=InvalidPartialEvaluator())
        outcome = await service.create(request)
        replay = await service.create(request.model_copy(update={"request_id": "replay"}))
        async with storage.uow() as uow:
            stored = await uow.eval_experiments.get("tenant-a", outcome.result.experiment_id)
    finally:
        await storage.dispose()

    assert outcome.result.status == "failed"
    assert replay.created is False
    assert replay.result.status == "failed"
    assert stored is not None
    assert stored.execution_claim_id is None
    assert stored.execution_claim_expires_at is None
