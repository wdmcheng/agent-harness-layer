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


__all__ = [
    "Any",
    "BoundaryCaseRefsEvaluator",
    "BoundaryRefsEvaluator",
    "CapturingPublisher",
    "InvalidPartialEvaluator",
    "LargeRawErrorEvaluator",
    "Path",
    "SplitAwareEvaluator",
    "UnsafeSuccessfulEvaluator",
    "ValidationError",
    "cast",
    "experiment_request",
    "json",
    "pytest",
    "seed_approved_cases",
    "sqlite_dsn",
]
