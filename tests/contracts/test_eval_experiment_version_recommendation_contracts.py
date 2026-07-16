"""评测版本、比较建议与本地证据合同测试。"""

from __future__ import annotations

from tests.contracts.test_eval_experiment_comparison_contracts import (
    _evaluation as _evaluation,
)
from tests.contracts.test_eval_experiment_comparison_contracts import (
    _harness_sources as _harness_sources,
)
from tests.contracts.test_eval_experiment_comparison_contracts import (
    json as json,
)
from tests.contracts.test_eval_experiment_comparison_contracts import (
    pytest as pytest,
)


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
        f"case-{index}": ("holdout", ["tool_selection"], 1.0, True) for index in range(6)
    }
    candidate_scores = {
        f"case-{index}": ("holdout", ["tool_selection"], 0.0, False) for index in range(6)
    }
    result = ExperimentComparisonBuilder(failure_inline_limit=2).build(
        experiment_id="experiment-large",
        requested_tags=["tool_selection"],
        baseline=_evaluation("baseline", baseline_scores),
        candidate=_evaluation("candidate", candidate_scores),
        regression_policy=RegressionPolicy(max_holdout_regression=0.0),
        authoritative_case_tags={f"case-{index}": ["tool_selection"] for index in range(6)},
    )

    assert len(result.new_failures) == 2
    assert result.failure_details_ref is not None
    assert result.failure_details_ref in result.local_evidence_refs
    assert len(result.failure_details["new_failures"]) == 6
    assert "failure_details" not in result.to_payload()
