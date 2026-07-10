"""Harness version、experiment runner 与 comparison 的公共合同测试。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal, cast

import pytest
from tests.contracts.auth_policy_hitl_contract_helpers import sqlite_dsn


def _harness_sources(*, tool_order: tuple[str, str] = ("search", "read")) -> dict[str, Any]:
    from agent_harness.evals import HarnessInputSource

    return {
        "prompt_instruction": HarnessInputSource(
            value={"system": "answer with evidence"},
            diff_summary="require evidence",
            evidence_ref="artifact://harness/prompt",
        ),
        "tool_descriptions": HarnessInputSource(
            value=[{"name": name, "description": f"{name} evidence"} for name in tool_order],
            evidence_ref="artifact://harness/tools",
        ),
        "agent_config": HarnessInputSource(value={"max_steps": 4}),
        "retrieval_config": HarnessInputSource(value={"top_k": 5, "collections": ["b", "a"]}),
        "policy_defaults": HarnessInputSource(value={"network": "deny"}),
        "model_adapter_settings": HarnessInputSource(
            value={"adapter": "fake", "profile": "deterministic"}
        ),
    }


def test_harness_version_is_complete_secret_safe_and_canonical() -> None:
    from agent_harness.evals import EvalExperimentError, HarnessVersionBuilder

    builder = HarnessVersionBuilder()
    first = builder.build(_harness_sources(tool_order=("search", "read")))
    second = builder.build(_harness_sources(tool_order=("read", "search")))

    assert first.version_id == second.version_id
    assert set(first.inputs) == {
        "prompt_instruction",
        "tool_descriptions",
        "agent_config",
        "retrieval_config",
        "policy_defaults",
        "model_adapter_settings",
    }
    assert all(len(item.checksum_sha256) == 64 for item in first.inputs.values())
    assert "answer with evidence" not in json.dumps(first.to_payload())
    with pytest.raises(ValueError):
        first.__class__.model_validate({**first.to_payload(), "version_id": "0" * 64})

    missing = _harness_sources()
    missing.pop("policy_defaults")
    with pytest.raises(EvalExperimentError) as incomplete:
        builder.build(missing)
    assert incomplete.value.code == "eval.harness.inputs_incomplete"

    secret = _harness_sources()
    secret["agent_config"].value = {"api_key": "sk-proj-secret123456789"}
    with pytest.raises(EvalExperimentError) as unsafe:
        builder.build(secret)
    assert unsafe.value.code == "eval.harness.input_secret"


@pytest.mark.parametrize(
    "unsafe_value",
    [object(), {"nested": object()}],
)
def test_harness_version_rejects_provider_objects_and_unsafe_refs(unsafe_value: object) -> None:
    from agent_harness.evals import EvalExperimentError, HarnessVersionBuilder

    sources = _harness_sources()
    sources["model_adapter_settings"].value = unsafe_value
    with pytest.raises(EvalExperimentError) as unserializable:
        HarnessVersionBuilder().build(sources)
    assert unserializable.value.code == "eval.harness.input_unserializable"

    sources = _harness_sources()
    sources["prompt_instruction"].evidence_ref = "/Users/alice/prompt.txt"
    with pytest.raises(EvalExperimentError) as unsafe_ref:
        HarnessVersionBuilder().build(sources)
    assert unsafe_ref.value.code == "eval.harness.evidence_ref_unsafe"


def _evaluation(
    version_id: str,
    scores: dict[str, tuple[str, list[str], float, bool]],
    *,
    local_refs: list[str] | None = None,
):
    from agent_harness.evals import ExperimentCaseResult, ExperimentEvaluationResult

    return ExperimentEvaluationResult(
        harness_version_id=version_id,
        evaluator_profile={"name": "exact-match", "version": "1"},
        metric_versions={"exact_match": "1"},
        case_results=[
            ExperimentCaseResult(
                case_id=case_id,
                subset=cast(
                    Literal["optimization", "holdout", "regression"], subset
                ),
                tags=tags,
                metric_scores={"exact_match": score},
                passed=passed,
                evidence_refs=[f"artifact://score/{version_id}/{case_id}"],
            )
            for case_id, (subset, tags, score, passed) in scores.items()
        ],
        local_evidence_refs=(
            [f"artifact://eval/{version_id}"] if local_refs is None else local_refs
        ),
    )


def test_comparison_reports_tags_holdout_failures_and_closed_recommendation() -> None:
    from agent_harness.evals import ExperimentComparisonBuilder, RegressionPolicy

    baseline = _evaluation(
        "baseline-v1",
        {
            "opt-tool": ("optimization", ["tool_selection"], 0.4, False),
            "opt-ret": ("optimization", ["retrieval_quality"], 0.6, True),
            "hold-tool": ("holdout", ["tool_selection"], 0.7, True),
            "hold-ret": ("holdout", ["retrieval_quality"], 0.7, True),
            "reg-critical": ("regression", ["policy_approval"], 1.0, True),
            "reg-fixed": ("regression", ["tool_selection"], 0.0, False),
        },
    )
    candidate = _evaluation(
        "candidate-v2",
        {
            "opt-tool": ("optimization", ["tool_selection"], 0.8, True),
            "opt-ret": ("optimization", ["retrieval_quality"], 0.7, True),
            "hold-tool": ("holdout", ["tool_selection"], 0.7, True),
            "hold-ret": ("holdout", ["retrieval_quality"], 0.69, True),
            "reg-critical": ("regression", ["policy_approval"], 1.0, True),
            "reg-fixed": ("regression", ["tool_selection"], 1.0, True),
        },
    )

    result = ExperimentComparisonBuilder().build(
        experiment_id="experiment-1",
        requested_tags=["tool_selection", "retrieval_quality"],
        baseline=baseline,
        candidate=candidate,
        regression_policy=RegressionPolicy(
            case_ids=["reg-fixed"],
            critical_case_ids=["reg-critical"],
            max_holdout_regression=0.02,
        ),
        authoritative_case_tags={
            "opt-tool": ["tool_selection"],
            "opt-ret": ["retrieval_quality"],
            "hold-tool": ["tool_selection"],
            "hold-ret": ["retrieval_quality"],
            "reg-critical": ["policy_approval"],
            "reg-fixed": ["tool_selection"],
        },
    )

    assert result.candidate_harness_version == "candidate-v2"
    assert {item.tag for item in result.per_tag} == {
        "tool_selection",
        "retrieval_quality",
    }
    assert result.holdout_delta == pytest.approx(-0.005)
    assert result.fixed_failures[0].case_id == "opt-tool"
    assert result.acceptance_recommendation == "accept"
    assert result.recommendation_reason_codes == [
        "target_tag_improved",
        "named_failure_fixed",
        "holdout_within_threshold",
        "critical_regression_passed",
    ]


def test_comparison_rejects_regressions_and_requires_local_evidence() -> None:
    from agent_harness.evals import BehaviorTag, ExperimentComparisonBuilder, RegressionPolicy

    baseline_scores = {
        "opt": ("optimization", ["tool_selection"], 0.5, True),
        "hold": ("holdout", ["tool_selection"], 0.9, True),
        "reg": ("regression", ["policy_approval"], 1.0, True),
    }
    candidate_scores = {
        "opt": ("optimization", ["tool_selection"], 0.8, True),
        "hold": ("holdout", ["tool_selection"], 0.6, False),
        "reg": ("regression", ["policy_approval"], 0.0, False),
    }
    builder = ExperimentComparisonBuilder()
    rejected = builder.build(
        experiment_id="experiment-2",
        requested_tags=["tool_selection"],
        baseline=_evaluation("baseline", baseline_scores),
        candidate=_evaluation("candidate", candidate_scores),
        regression_policy=RegressionPolicy(
            critical_tags=[BehaviorTag.POLICY_APPROVAL], max_holdout_regression=0.1
        ),
        authoritative_case_tags={
            "opt": ["tool_selection"],
            "hold": ["tool_selection"],
            "reg": ["policy_approval"],
        },
    )
    assert rejected.acceptance_recommendation == "reject"
    assert "holdout_regression_exceeded" in rejected.recommendation_reason_codes
    assert "critical_regression_failed" in rejected.recommendation_reason_codes
    assert "new_failures_present" in rejected.recommendation_reason_codes

    incomplete = builder.build(
        experiment_id="experiment-3",
        requested_tags=["tool_selection"],
        baseline=_evaluation("baseline", baseline_scores, local_refs=[]),
        candidate=_evaluation("candidate", candidate_scores),
        regression_policy=RegressionPolicy(critical_tags=[BehaviorTag.POLICY_APPROVAL]),
        authoritative_case_tags={
            "opt": ["tool_selection"],
            "hold": ["tool_selection"],
            "reg": ["policy_approval"],
        },
    )
    assert incomplete.acceptance_recommendation == "needs_review"
    assert incomplete.recommendation_reason_codes == [
        "local_evidence_incomplete",
        "comparison_incomplete",
    ]

    missing_critical_tag = _evaluation("candidate", candidate_scores)
    missing_critical_tag.case_results[-1].tags = []
    tag_mismatch = builder.build(
        experiment_id="experiment-tag-mismatch",
        requested_tags=["tool_selection"],
        baseline=_evaluation("baseline", baseline_scores),
        candidate=missing_critical_tag,
        regression_policy=RegressionPolicy(critical_tags=[BehaviorTag.POLICY_APPROVAL]),
        authoritative_case_tags={
            "opt": ["tool_selection"],
            "hold": ["tool_selection"],
            "reg": ["policy_approval"],
        },
    )
    assert tag_mismatch.acceptance_recommendation == "needs_review"
    assert "comparison_incomplete" in tag_mismatch.recommendation_reason_codes

    metric_mismatch = _evaluation("candidate", candidate_scores)
    metric_mismatch.case_results[0].metric_scores = {"other_metric": 0.8}
    incomparable = builder.build(
        experiment_id="experiment-metric-mismatch",
        requested_tags=["tool_selection"],
        baseline=_evaluation("baseline", baseline_scores),
        candidate=metric_mismatch,
        regression_policy=RegressionPolicy(),
        authoritative_case_tags={
            "opt": ["tool_selection"],
            "hold": ["tool_selection"],
            "reg": ["policy_approval"],
        },
    )
    assert incomparable.acceptance_recommendation == "needs_review"
    assert "comparison_incomplete" in incomparable.recommendation_reason_codes


def test_large_failure_diff_is_truncated_to_local_evidence_ref() -> None:
    from agent_harness.evals import ExperimentComparisonBuilder, RegressionPolicy

    baseline_scores = {
        f"case-{index}": ("holdout", ["tool_selection"], 1.0, True)
        for index in range(6)
    }
    candidate_scores = {
        f"case-{index}": ("holdout", ["tool_selection"], 0.0, False)
        for index in range(6)
    }
    result = ExperimentComparisonBuilder(failure_inline_limit=2).build(
        experiment_id="experiment-large",
        requested_tags=["tool_selection"],
        baseline=_evaluation("baseline", baseline_scores),
        candidate=_evaluation("candidate", candidate_scores),
        regression_policy=RegressionPolicy(max_holdout_regression=0.0),
        authoritative_case_tags={
            f"case-{index}": ["tool_selection"] for index in range(6)
        },
    )

    assert len(result.new_failures) == 2
    assert result.failure_details_ref is not None
    assert result.failure_details_ref in result.local_evidence_refs
    assert len(result.failure_details["new_failures"]) == 6
    assert "failure_details" not in result.to_payload()


class RecordingEvaluator:
    def __init__(
        self,
        results: dict[str, Any],
        *,
        fail_candidate: bool = False,
        partial_candidate: Any | None = None,
    ) -> None:
        self.results = results
        self.fail_candidate = fail_candidate
        self.partial_candidate = partial_candidate
        self.calls: list[dict[str, Any]] = []

    async def evaluate(self, **kwargs: Any):
        self.calls.append(kwargs)
        version = kwargs["harness_version"].version_id
        if (
            self.fail_candidate
            and self.partial_candidate is not None
            and version == self.partial_candidate.harness_version_id
        ):
            from agent_harness.evals import ExperimentEvaluationFailure

            raise ExperimentEvaluationFailure(
                "provider failed Authorization: Bearer evaluator-secret",
                partial_result=self.partial_candidate,
            )
        return self.results[version]


class FailingPublisher:
    provider_name = "optional-provider"

    async def publish(self, payload: dict[str, Any]) -> dict[str, object]:
        del payload
        raise RuntimeError("Authorization: Bearer provider-secret")


@pytest.mark.asyncio
async def test_experiment_service_reuses_split_persists_local_first_and_degrades_provider(
    tmp_path: Path,
) -> None:
    from agent_harness.evals import ExperimentRequest, ExperimentService, HarnessVersionBuilder
    from agent_harness.storage import EvalDatasetSplitCreate, SQLAlchemyStorage, run_migrations

    dsn = sqlite_dsn(tmp_path / "experiment-service.db")
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    baseline = HarnessVersionBuilder().build(_harness_sources())
    candidate_sources = _harness_sources()
    candidate_sources["prompt_instruction"].value = {"system": "answer with cited evidence"}
    candidate = HarnessVersionBuilder().build(candidate_sources)
    scores = {
        baseline.version_id: _evaluation(
            baseline.version_id,
            {
                "opt": ("optimization", ["tool_selection"], 0.5, True),
                "hold": ("holdout", ["tool_selection"], 0.8, True),
            },
        ),
        candidate.version_id: _evaluation(
            candidate.version_id,
            {
                "opt": ("optimization", ["tool_selection"], 0.7, True),
                "hold": ("holdout", ["tool_selection"], 0.8, True),
            },
        ),
    }
    evaluator = RecordingEvaluator(scores)
    try:
        async with storage.uow() as uow:
            await uow.tenants.ensure("tenant-a")
            await uow.eval_dataset_splits.create(
                EvalDatasetSplitCreate(
                    split_id="split-1",
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
            await uow.commit()

        service = ExperimentService(
            storage=storage,
            evaluator=evaluator,
            publishers=[FailingPublisher()],
        )
        result = await service.run(
            ExperimentRequest(
                request_id="request-1",
                tenant_id="tenant-a",
                idempotency_key="experiment-key",
                agent_id="examples.basic",
                dataset="default",
                split_id="split-1",
                baseline_harness_version=baseline,
                candidate_harness_version=candidate,
                evaluator_profile={"name": "exact-match", "version": "1"},
                metric_versions={"exact_match": "1"},
            )
        )

        assert result.status == "completed_with_degradation"
        assert result.comparison is not None
        assert result.comparison.acceptance_recommendation == "accept"
        assert result.local_evidence_refs
        assert result.provider_statuses[0]["status"] == "degraded"
        assert "provider-secret" not in json.dumps(result.to_payload())
        assert len(evaluator.calls) == 2
        assert evaluator.calls[0]["split"].split_id == evaluator.calls[1]["split"].split_id
        assert evaluator.calls[0]["evaluator_profile"] == evaluator.calls[1]["evaluator_profile"]

        replay = await service.run(
            ExperimentRequest(
                request_id="request-retry",
                tenant_id="tenant-a",
                idempotency_key="experiment-key",
                agent_id="examples.basic",
                dataset="default",
                split_id="split-1",
                baseline_harness_version=baseline,
                candidate_harness_version=candidate,
                evaluator_profile={"name": "exact-match", "version": "1"},
                metric_versions={"exact_match": "1"},
            )
        )
        assert replay.experiment_id == result.experiment_id
        assert len(evaluator.calls) == 2
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_baseline_only_cross_tenant_and_partial_failure_are_fail_closed(
    tmp_path: Path,
) -> None:
    from agent_harness.evals import (
        EvalExperimentError,
        ExperimentRequest,
        ExperimentService,
        HarnessVersionBuilder,
    )
    from agent_harness.storage import EvalDatasetSplitCreate, SQLAlchemyStorage, run_migrations

    dsn = sqlite_dsn(tmp_path / "experiment-boundaries.db")
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    baseline = HarnessVersionBuilder().build(_harness_sources())
    candidate_sources = _harness_sources()
    candidate_sources["prompt_instruction"].value = {"system": "candidate prompt"}
    candidate = HarnessVersionBuilder().build(candidate_sources)
    baseline_result = _evaluation(
        baseline.version_id,
        {
            "opt": ("optimization", ["tool_selection"], 0.5, True),
            "hold": ("holdout", ["tool_selection"], 0.8, True),
        },
    )
    partial_candidate = _evaluation(
        candidate.version_id,
        {"opt": ("optimization", ["tool_selection"], 0.7, True)},
    )
    evaluator = RecordingEvaluator(
        {baseline.version_id: baseline_result},
        fail_candidate=True,
        partial_candidate=partial_candidate,
    )
    try:
        async with storage.uow() as uow:
            await uow.tenants.ensure("tenant-a")
            await uow.tenants.ensure("tenant-b")
            await uow.eval_dataset_splits.create(
                EvalDatasetSplitCreate(
                    split_id="split-boundaries",
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
            await uow.commit()

        service = ExperimentService(storage=storage, evaluator=evaluator)
        baseline_only = await service.run(
            ExperimentRequest(
                request_id="baseline-request",
                tenant_id="tenant-a",
                idempotency_key="baseline-only",
                agent_id="examples.basic",
                dataset="default",
                split_id="split-boundaries",
                baseline_harness_version=baseline,
                evaluator_profile={"name": "exact-match", "version": "1"},
                metric_versions={"exact_match": "1"},
            )
        )
        assert baseline_only.status == "baseline_completed"
        with pytest.raises(EvalExperimentError) as missing:
            await service.compare(
                tenant_id="tenant-a",
                experiment_id=baseline_only.experiment_id,
                request_id="compare-request",
            )
        assert missing.value.code == "eval.experiment.candidate_missing"

        with pytest.raises(EvalExperimentError) as hidden:
            await service.get(
                tenant_id="tenant-b",
                experiment_id=baseline_only.experiment_id,
                request_id="hidden-request",
            )
        assert hidden.value.code == "eval.experiment.not_found"

        failed = await service.run(
            ExperimentRequest(
                request_id="failed-request",
                tenant_id="tenant-a",
                idempotency_key="candidate-fails",
                agent_id="examples.basic",
                dataset="default",
                split_id="split-boundaries",
                baseline_harness_version=baseline,
                candidate_harness_version=candidate,
                evaluator_profile={"name": "exact-match", "version": "1"},
                metric_versions={"exact_match": "1"},
            )
        )
        assert failed.status == "failed"
        assert failed.local_evidence_refs
        async with storage.uow() as uow:
            stored = await uow.eval_experiments.get("tenant-a", failed.experiment_id)
        assert stored is not None
        assert "evaluator-secret" not in json.dumps(stored.score_summaries)
        assert stored.score_summaries["baseline"]["case_results"]
        assert stored.score_summaries["candidate"]["case_results"][0]["case_id"] == "opt"
    finally:
        await storage.dispose()
