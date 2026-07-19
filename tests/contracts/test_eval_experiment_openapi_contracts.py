"""EVL-004 运行时 OpenAPI 精确漂移合同。"""

from __future__ import annotations

from pathlib import Path
from typing import cast

from agent_harness.events import LocalJsonlEventSink
from agent_harness.runtime import RunOrchestrator
from app.main import create_app


def test_eval_experiment_openapi_is_exact_and_stable(tmp_path: Path) -> None:
    """固定评测实验 API 的安全、响应码和 DTO 必填字段，阻止文档静默漂移。

    这里校验的是公开协议，而非框架生成细节；新增字段或状态必须先经过
    明确的契约演进，避免客户端依据过期 OpenAPI 发送或解析请求。
    """
    app = create_app(
        orchestrator=cast(RunOrchestrator, object()),
        event_sink=LocalJsonlEventSink(tmp_path / "openapi-events.jsonl"),
    )
    schema = app.openapi()
    paths = schema["paths"]
    operations = {
        ("/api/v1/evals/experiments", "post"): {
            "200",
            "201",
            "401",
            "403",
            "404",
            "409",
            "422",
            "500",
        },
        ("/api/v1/evals/experiments/{experiment_id}", "get"): {
            "200",
            "401",
            "403",
            "404",
            "500",
        },
        ("/api/v1/evals/experiments/{experiment_id}/comparison", "get"): {
            "200",
            "401",
            "403",
            "404",
            "409",
            "500",
        },
        ("/api/v1/evals/experiments/{experiment_id}/accept", "post"): {
            "200",
            "401",
            "403",
            "404",
            "409",
            "422",
            "500",
        },
    }
    for (path, method), expected_responses in operations.items():
        operation = paths[path][method]
        assert {"HTTPBearer": []} in operation["security"]
        assert set(operation["responses"]) == expected_responses
        for status in expected_responses - {"200", "201"}:
            error_schema = operation["responses"][status]["content"]["application/json"]["schema"]
            assert error_schema["$ref"].endswith("/ApiErrorEnvelope")

    create_parameters = paths["/api/v1/evals/experiments"]["post"]["parameters"]
    key = next(item for item in create_parameters if item["name"] == "Idempotency-Key")
    assert key["in"] == "header"
    assert key["required"] is True
    component_names = set(schema["components"]["schemas"])
    assert {
        "EvalExperimentCreateRequest",
        "EvalExperimentResponse",
        "EvalExperimentComparisonResponse",
        "EvalExperimentAcceptanceRequest",
        "EvalExperimentAcceptanceResponse",
    }.issubset(component_names)
    components = schema["components"]["schemas"]
    assert set(components["EvalExperimentResponse"]["properties"]["status"]["enum"]) == {
        "running",
        "baseline_completed",
        "completed",
        "failed",
        "needs_review",
        "baseline_completed_with_degradation",
        "completed_with_degradation",
    }
    assert set(components["EvalExperimentCreateRequest"]["required"]) == {
        "agent_id",
        "dataset",
        "tags",
        "split_strategy",
        "baseline_harness_version",
    }
    assert set(components["EvalExperimentResponse"]["required"]) == {
        "request_id",
        "experiment_id",
        "status",
        "agent_id",
        "dataset",
        "tags",
        "optimization_case_count",
        "holdout_case_count",
        "regression_case_count",
        "baseline_harness_version",
        "baseline_eval_run_ref",
        "local_evidence_refs",
        "provider_statuses",
    }
    assert set(components["EvalExperimentAcceptanceRequest"]["required"]) == {
        "decision",
        "reason",
    }
    assert set(components["EvalExperimentComparisonResponse"]["required"]) == {
        "request_id",
        "experiment_id",
        "candidate_harness_version",
        "per_tag",
        "holdout_delta",
        "regressions",
        "new_failures",
        "fixed_failures",
        "acceptance_recommendation",
        "recommendation_reason_codes",
        "local_evidence_refs",
        "provider_statuses",
    }
    assert set(components["EvalExperimentAcceptanceResponse"]["required"]) == {
        "request_id",
        "experiment_id",
        "decision_id",
        "decision",
        "reviewer_id",
        "production_binding",
        "policy_decision",
        "audit_ref",
        "evidence_refs",
    }
    reason_schema = schema["components"]["schemas"]["EvalExperimentComparisonResponse"][
        "properties"
    ]["recommendation_reason_codes"]
    assert reason_schema["minItems"] == 1
    assert set(reason_schema["items"]["enum"]) == {
        "target_tag_improved",
        "named_failure_fixed",
        "no_target_improvement",
        "holdout_within_threshold",
        "holdout_regression_exceeded",
        "critical_regression_passed",
        "critical_regression_failed",
        "new_failures_present",
        "local_evidence_incomplete",
        "comparison_incomplete",
    }
