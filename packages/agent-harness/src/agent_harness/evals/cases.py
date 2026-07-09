"""Eval case DTO 与 trace 到 draft 的 factory。"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import Field

from agent_harness.contracts.dto import HarnessDTO
from agent_harness.security.redaction import redact_secrets
from agent_harness.storage import EvalCaseCreate


class EvalTraceSource(HarnessDTO):
    """从 failed/low-score trace 生成 draft case 所需的最小输入。"""

    tenant_id: str
    agent_id: str
    run_id: str | None = None
    trace_id: str | None = None
    trigger: str = "failed_run"
    input: dict[str, Any] = Field(default_factory=dict)
    output: dict[str, Any] | None = None
    expected: dict[str, Any] | None = None
    source_refs: list[str] = Field(default_factory=list)
    artifact_refs: list[str] = Field(default_factory=list)
    scores: dict[str, float] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class EvalCaseFactory:
    """把 trace source 收敛成 draft case DTO，不直接写 storage。"""

    def __init__(self, *, inline_payload_bytes: int = 8192) -> None:
        self._inline_payload_bytes = inline_payload_bytes

    def create_draft(
        self,
        source: EvalTraceSource,
        *,
        name: str | None = None,
        dataset: str = "default",
    ) -> EvalCaseCreate:
        """生成 draft case；自动路径不能产生 approved 状态。"""

        payload = redact_secrets(
            {
                "input": source.input,
                "output": source.output,
                "expected": source.expected,
                "scores": source.scores,
                "trigger": source.trigger,
            }
        )
        payload, artifact_refs = _externalize_large_payload(
            payload=payload,
            artifact_refs=source.artifact_refs,
            inline_payload_bytes=self._inline_payload_bytes,
        )
        metadata = redact_secrets(
            {
                **source.metadata,
                "trace": {
                    "run_id": source.run_id,
                    "trace_id": source.trace_id,
                    "trigger": source.trigger,
                },
            }
        )
        return EvalCaseCreate(
            tenant_id=source.tenant_id,
            agent_id=source.agent_id,
            run_id=source.run_id,
            trace_id=source.trace_id,
            name=name or f"{source.agent_id}:{source.trigger}:{source.trace_id or source.run_id}",
            trigger=source.trigger,
            dataset=dataset,
            source_refs=source.source_refs,
            artifact_refs=artifact_refs,
            payload=payload,
            metadata=metadata,
        )


class EvalDraftDetector:
    """把 failed run 或低分 signal 判定为 draft eval case。"""

    def __init__(
        self,
        *,
        factory: EvalCaseFactory | None = None,
        low_score_threshold: float = 0.8,
    ) -> None:
        self._factory = factory or EvalCaseFactory()
        self._low_score_threshold = low_score_threshold

    def detect(
        self,
        source: EvalTraceSource,
        *,
        dataset: str = "default",
        name: str | None = None,
        score_threshold: float | None = None,
    ) -> EvalCaseCreate | None:
        """低分 signal 低于阈值时生成 low_score draft；failed/manual 直接成 draft。"""

        threshold = self._low_score_threshold if score_threshold is None else score_threshold
        low_scores = {metric: value for metric, value in source.scores.items() if value < threshold}
        if low_scores:
            return self._factory.create_draft(
                source.model_copy(
                    update={
                        "trigger": "low_score",
                        "metadata": {
                            **source.metadata,
                            "score_signal": {
                                "threshold": threshold,
                                "scores": source.scores,
                                "low_scores": low_scores,
                            },
                        },
                    }
                ),
                name=name,
                dataset=dataset,
            )
        if source.trigger in {"failed_run", "manual"}:
            return self._factory.create_draft(source, name=name, dataset=dataset)
        return None


def _externalize_large_payload(
    *,
    payload: dict[str, Any],
    artifact_refs: list[str],
    inline_payload_bytes: int,
) -> tuple[dict[str, Any], list[str]]:
    payload_bytes = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    if len(payload_bytes) <= inline_payload_bytes:
        return payload, artifact_refs
    checksum = hashlib.sha256(payload_bytes).hexdigest()
    ref = f"payload://sha256/{checksum}"
    summary = {
        "payload_ref": ref,
        "payload_omitted": {
            "size_bytes": len(payload_bytes),
            "checksum_sha256": checksum,
            "reason": "eval_case_inline_limit",
        },
    }
    return summary, [*artifact_refs, ref]
