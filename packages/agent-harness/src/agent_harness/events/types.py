"""Canonical event envelope and event type definitions."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import Field

from agent_harness.contracts.dto import HarnessDTO


class CanonicalEventType(StrEnum):
    RUN_QUEUED = "run.queued"
    RUN_STARTED = "run.started"
    RUN_RESUMED = "run.resumed"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    RUN_CANCELLED = "run.cancelled"
    MODEL_REQUEST_STARTED = "model.request.started"
    MODEL_OUTPUT_DELTA = "model.output.delta"
    MODEL_OUTPUT_COMPLETED = "model.output.completed"
    MODEL_STRUCTURED_DELTA = "model.structured.delta"
    MODEL_STRUCTURED_COMPLETED = "model.structured.completed"
    MODEL_USAGE_UPDATED = "model.usage.updated"
    INPUT_GUARDRAIL_CHECKED = "input.guardrail.checked"
    INPUT_GUARDRAIL_BLOCKED = "input.guardrail.blocked"
    REASONING_DELTA = "reasoning.delta"
    TOOL_CALL_ARGS_DELTA = "tool.call.args_delta"
    TOOL_CALL_STARTED = "tool.call.started"
    TOOL_CALL_COMPLETED = "tool.call.completed"
    TOOL_CALL_FAILED = "tool.call.failed"
    RETRIEVAL_QUERY_STARTED = "retrieval.query.started"
    RETRIEVAL_QUERY_COMPLETED = "retrieval.query.completed"
    CONTEXT_ASSEMBLY_STARTED = "context.assembly.started"
    CONTEXT_ASSEMBLY_COMPLETED = "context.assembly.completed"
    POLICY_DECISION = "policy.decision"
    APPROVAL_REQUIRED = "approval.required"
    APPROVAL_RESOLVED = "approval.resolved"
    CHECKPOINT_CREATED = "checkpoint.created"
    CONTEXT_COMPACTION_STARTED = "context.compaction.started"
    CONTEXT_COMPACTION_COMPLETED = "context.compaction.completed"
    EVAL_CASE_DRAFTED = "eval.case.drafted"
    EVAL_CASE_APPROVED = "eval.case.approved"
    EVAL_RUN_STARTED = "eval.run.started"
    EVAL_RUN_COMPLETED = "eval.run.completed"
    EVAL_SCORE_RECORDED = "eval.score.recorded"
    ARTIFACT_CREATED = "artifact.created"


class CanonicalEvent(HarnessDTO):
    """跨 runtime、API、SSE、trace 和 eval 的稳定事件 envelope。"""

    event_id: str = Field(default_factory=lambda: str(uuid4()))
    tenant_id: str
    run_id: str
    user_id: str | None = None
    agent_id: str | None = None
    parent_run_id: str | None = None
    event_type: CanonicalEventType
    event_version: str = "1.0"
    seq: int
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    payload: dict[str, Any] | None = None
    payload_ref: str | None = None
    payload_checksum: str | None = None
    raw_event_ref: str | None = None
    terminal: bool = False
    visibility: str = "internal"
    request_id: str | None = None
    trace_id: str | None = None
    span_id: str | None = None
