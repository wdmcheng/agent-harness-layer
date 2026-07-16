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
                subset=cast(Literal["optimization", "holdout", "regression"], subset),
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


__all__ = [
    "Any",
    "FailingPublisher",
    "Literal",
    "Path",
    "RecordingEvaluator",
    "_evaluation",
    "_harness_sources",
    "cast",
    "json",
    "pytest",
    "sqlite_dsn",
]
