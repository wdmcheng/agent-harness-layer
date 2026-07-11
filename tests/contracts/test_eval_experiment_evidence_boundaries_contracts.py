"""Experiment evidence 派生边界与真实 create split 合同。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError
from tests.contracts.auth_policy_hitl_contract_helpers import sqlite_dsn
from tests.contracts.test_eval_experiment_api_contracts import SplitAwareEvaluator
from tests.contracts.test_eval_experiment_recovery_contracts import (
    experiment_request,
    seed_approved_cases,
)


class BoundaryRefsEvaluator(SplitAwareEvaluator):
    def __init__(
        self,
        baseline_id: str,
        candidate_id: str,
        refs_by_version: dict[str, list[str]],
    ) -> None:
        super().__init__(baseline_id, candidate_id)
        self.refs_by_version = refs_by_version

    async def evaluate(self, **kwargs: Any):
        result = await super().evaluate(**kwargs)
        result.local_evidence_refs = list(self.refs_by_version[result.harness_version_id])
        return result


class CapturingPublisher:
    provider_name = "bounded-evidence-test"

    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    async def publish(self, payload: dict[str, Any]) -> dict[str, object]:
        self.payloads.append(payload)
        return {"status": "completed", "evidence_refs": []}


class BoundaryCaseRefsEvaluator(SplitAwareEvaluator):
    def __init__(
        self,
        baseline_id: str,
        candidate_id: str,
        *,
        ref_count: int,
        ref_padding: int,
    ) -> None:
        super().__init__(baseline_id, candidate_id)
        self.ref_count = ref_count
        self.ref_padding = ref_padding

    async def evaluate(self, **kwargs: Any):
        result = await super().evaluate(**kwargs)
        item = result.case_results[0]
        baseline = result.harness_version_id == self.baseline_id
        item.metric_scores = {"exact_match": 1.0 if baseline else 0.0}
        item.passed = baseline
        side = "baseline" if baseline else "candidate"
        item.evidence_refs = [
            f"artifact://bounded-case/{side}/{index}/" + "x" * self.ref_padding
            for index in range(self.ref_count)
        ]
        return result


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


class InvalidPartialEvaluator:
    async def evaluate(self, **kwargs: Any):
        from agent_harness.evals import (
            ExperimentCaseResult,
            ExperimentEvaluationFailure,
            ExperimentEvaluationResult,
        )

        version_id = kwargs["harness_version"].version_id
        partial = ExperimentEvaluationResult(
            harness_version_id=version_id,
            evaluator_profile=kwargs["evaluator_profile"],
            metric_versions=kwargs["metric_versions"],
            case_results=[
                ExperimentCaseResult(
                    case_id="not-in-frozen-split",
                    subset="holdout",
                    tags=["tool_selection"],
                    metric_scores={"exact_match": 0.0},
                    passed=False,
                )
            ],
        )
        raise ExperimentEvaluationFailure("partial failure", partial_result=partial)


class LargeRawErrorEvaluator:
    async def evaluate(self, **_kwargs: Any):
        raise RuntimeError("provider raw response:" + "x" * 1_000_000)


class UnsafeSuccessfulEvaluator(SplitAwareEvaluator):
    def __init__(self, baseline_id: str, candidate_id: str, unsafe_mode: str) -> None:
        super().__init__(baseline_id, candidate_id)
        self.unsafe_mode = unsafe_mode

    async def evaluate(self, **kwargs: Any):
        result = await super().evaluate(**kwargs)
        if self.unsafe_mode == "secret":
            result.case_results[0].evidence_refs = ["api_key=successful-evaluator-secret-123456"]
        elif self.unsafe_mode == "oversized":
            result.case_results[0].evidence_refs = ["x" * 1_000_000]
        elif self.unsafe_mode == "oversized_list":
            result.case_results[0].evidence_refs = [
                f"artifact://mutated/{index}/" + "x" * 150 for index in range(100)
            ]
        else:
            result.local_evidence_refs = ["/Users/alice/private-evaluator-result.json"]
        return result


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


@pytest.mark.asyncio
async def test_evaluator_raw_error_is_replaced_by_bounded_structured_summary(
    tmp_path: Path,
) -> None:
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
