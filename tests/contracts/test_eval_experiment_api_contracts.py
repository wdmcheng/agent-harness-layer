"""EVL-004 HTTP、OpenAPI、tenant、幂等与 side-effect 合同。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Literal, cast

import pytest
from tests.contracts.auth_policy_hitl_contract_helpers import (
    asgi_request,
    sqlite_dsn,
    table_count,
)

from agent_harness.events import LocalJsonlEventSink
from agent_harness.identity import IdentityContext
from agent_harness.runtime import RunOrchestrator
from app.main import create_app


def experiment_manifest(seed: str):
    from agent_harness.evals import HarnessInputSource, HarnessVersionBuilder

    return HarnessVersionBuilder().build(
        {
            "prompt_instruction": HarnessInputSource(value={"prompt": seed}),
            "tool_descriptions": HarnessInputSource(value=[]),
            "agent_config": HarnessInputSource(value={"max_steps": 4}),
            "retrieval_config": HarnessInputSource(value={"top_k": 5}),
            "policy_defaults": HarnessInputSource(value={"network": "deny"}),
            "model_adapter_settings": HarnessInputSource(value={"adapter": "fake"}),
        }
    )


class SplitAwareEvaluator:
    def __init__(self, baseline_id: str, candidate_id: str) -> None:
        self.baseline_id = baseline_id
        self.candidate_id = candidate_id
        self.calls: list[str] = []

    async def evaluate(self, **kwargs: Any):
        from agent_harness.evals import ExperimentCaseResult, ExperimentEvaluationResult

        split = kwargs["split"]
        version_id = kwargs["harness_version"].version_id
        self.calls.append(version_id)
        cases: list[ExperimentCaseResult] = []
        subsets: dict[str, Literal["optimization", "holdout", "regression"]] = {
            **{case_id: "optimization" for case_id in split.optimization_case_ids},
            **{case_id: "holdout" for case_id in split.holdout_case_ids},
            **{case_id: "regression" for case_id in split.regression_case_ids},
        }
        for case_id, subset in subsets.items():
            score = 0.8
            if subset == "optimization":
                score = 0.5 if version_id == self.baseline_id else 0.9
            cases.append(
                ExperimentCaseResult(
                    case_id=case_id,
                    subset=subset,
                    tags=split.case_tags[case_id],
                    metric_scores={"exact_match": score},
                    passed=True,
                    evidence_refs=[f"artifact://experiment/{version_id}/{case_id}"],
                )
            )
        return ExperimentEvaluationResult(
            harness_version_id=version_id,
            evaluator_profile=kwargs["evaluator_profile"],
            metric_versions=kwargs["metric_versions"],
            case_results=cases,
            local_evidence_refs=[f"artifact://experiment/{version_id}"],
        )


class FailingPublisher:
    provider_name = "optional-eval-provider"

    async def publish(self, payload: dict[str, Any]) -> dict[str, object]:
        del payload
        raise RuntimeError("Authorization: Bearer provider-secret-123456")


class UnsafePublisher:
    provider_name = "unsafe-provider"

    async def publish(self, payload: dict[str, Any]) -> dict[str, object]:
        del payload
        return {
            "provider": "spoofed-provider",
            "status": "completed",
            "detail": "x" * 20_000,
            "raw_response": "provider raw body",
            "evidence_refs": ["/Users/alice/private-provider.json"],
        }


class BlockingEvaluator(SplitAwareEvaluator):
    def __init__(self, baseline_id: str, candidate_id: str) -> None:
        super().__init__(baseline_id, candidate_id)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def evaluate(self, **kwargs: Any):
        if not self.calls:
            self.started.set()
            await self.release.wait()
        return await super().evaluate(**kwargs)


class ExplodingExperimentService:
    def __init__(self) -> None:
        self.calls = 0

    async def create(self, _request: object) -> object:
        self.calls += 1
        raise RuntimeError("Authorization: Bearer internal-provider-secret")


async def seed_approved_cases(storage: Any) -> None:
    from agent_harness.storage import EvalCaseCreate

    async with storage.uow() as uow:
        await uow.tenants.ensure("tenant-a")
        for index in range(3):
            case = await uow.eval_cases.create(
                EvalCaseCreate(
                    tenant_id="tenant-a",
                    agent_id="examples.basic",
                    name=f"case-{index}",
                    payload={"output": {"answer": index}, "expected": {"answer": index}},
                    metadata={"behavior_tags": ["tool_selection", "retrieval_quality"]},
                )
            )
            await uow.eval_cases.approve(
                case_id=case.case_id,
                tenant_id="tenant-a",
                approved_by="curator",
                reason="safe tagged fixture",
            )
        await uow.commit()


def experiment_body() -> tuple[dict[str, object], str]:
    baseline = experiment_manifest("baseline")
    candidate = experiment_manifest("candidate")
    return (
        {
            "agent_id": "examples.basic",
            "dataset": "default",
            "tags": ["tool_selection"],
            "split_strategy": "deterministic_multilabel_v1",
            "baseline_harness_version": baseline.to_payload(),
            "candidate_harness_version": candidate.to_payload(),
        },
        candidate.version_id,
    )


__all__ = [
    "Any",
    "BlockingEvaluator",
    "ExplodingExperimentService",
    "FailingPublisher",
    "IdentityContext",
    "Literal",
    "LocalJsonlEventSink",
    "Path",
    "RunOrchestrator",
    "SplitAwareEvaluator",
    "UnsafePublisher",
    "asgi_request",
    "asyncio",
    "cast",
    "create_app",
    "experiment_body",
    "experiment_manifest",
    "json",
    "pytest",
    "seed_approved_cases",
    "sqlite_dsn",
    "table_count",
]
