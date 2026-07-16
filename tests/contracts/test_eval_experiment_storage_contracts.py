"""Eval experiment migration 与 repository 合同测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from tests.contracts.auth_policy_hitl_contract_helpers import sqlite_dsn

from agent_harness.storage import (
    EvalDatasetSplitCreate,
    EvalExperimentCreate,
    HarnessAcceptanceCreate,
)


def split_create() -> EvalDatasetSplitCreate:
    return EvalDatasetSplitCreate(
        split_id="split-1",
        tenant_id="tenant-a",
        agent_id="examples.basic",
        dataset="default",
        request_id="request-1",
        tags=["retrieval_quality", "tool_selection"],
        strategy="deterministic_multilabel_v1",
        optimization_ratio=0.8,
        holdout_ratio=0.2,
        regression_policy={"case_ids": ["case-r"]},
        case_tags={
            "case-o": ["retrieval_quality"],
            "case-h": ["retrieval_quality"],
            "case-r": ["policy_approval"],
        },
        optimization_case_ids=["case-o"],
        holdout_case_ids=["case-h"],
        regression_case_ids=["case-r"],
        tag_distribution={"retrieval_quality": {"optimization": 1, "holdout": 1}},
        rejected_counts={"draft": 2},
        evidence_refs=["artifact://split-1"],
    )


def experiment_create(*, request_hash: str = "a" * 64) -> EvalExperimentCreate:
    return EvalExperimentCreate(
        tenant_id="tenant-a",
        idempotency_key="experiment-key",
        request_hash=request_hash,
        request_id="request-1",
        agent_id="examples.basic",
        dataset="default",
        split_id="split-1",
        evaluator_profile={"name": "exact-match", "version": "1"},
        metric_versions={"exact_match": "1"},
        baseline_harness={"version": "baseline-v1"},
        candidate_harness={"version": "candidate-v2"},
    )


def acceptance_create(*, request_hash: str = "b" * 64) -> HarnessAcceptanceCreate:
    return HarnessAcceptanceCreate(
        tenant_id="tenant-a",
        experiment_id="experiment-1",
        decision_request_hash=request_hash,
        reviewer_id="reviewer-1",
        reason="holdout and regression evidence reviewed",
        decision="accepted",
        accepted_harness_version="candidate-v2",
        production_binding={"agent_id": "examples.basic", "version": "candidate-v2"},
        policy_decision={"decision": "allow", "reason": "manual review allowed"},
        audit_ref="audit://acceptance-1",
        evidence_refs=["artifact://comparison-1"],
    )


__all__ = [
    "EvalDatasetSplitCreate",
    "EvalExperimentCreate",
    "HarnessAcceptanceCreate",
    "Path",
    "UTC",
    "ValidationError",
    "acceptance_create",
    "datetime",
    "experiment_create",
    "pytest",
    "split_create",
    "sqlite_dsn",
    "timedelta",
]
