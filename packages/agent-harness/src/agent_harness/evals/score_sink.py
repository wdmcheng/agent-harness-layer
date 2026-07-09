"""Eval score sink：local-first，再 fan-out 到观测 provider。"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import Field

from agent_harness.contracts.dto import HarnessDTO
from agent_harness.events import CanonicalEventType
from agent_harness.observability import (
    TelemetryContext,
    TelemetryFacade,
    TelemetryRecord,
    TelemetryStatus,
)
from agent_harness.security.redaction import redact_secrets
from agent_harness.storage import EvalScoreCreate


def _empty_telemetry_statuses() -> list[TelemetryStatus]:
    return []


class ScoreSinkResult(HarnessDTO):
    """ScoreSink 写入结果，provider 失败只表达为 degraded 状态。"""

    local_status: str
    provider_statuses: list[TelemetryStatus] = Field(default_factory=_empty_telemetry_statuses)
    local_ref: str | None = None


class ScoreSink:
    """先写本地 JSONL score，再发布 provider-neutral telemetry record。"""

    def __init__(self, *, local_path: Path, telemetry: TelemetryFacade | None = None) -> None:
        self._local_path = local_path
        self._telemetry = telemetry

    async def write_score(self, score: EvalScoreCreate) -> ScoreSinkResult:
        """写入已脱敏 score evidence；provider failure 不抛给 eval runner。"""

        payload = redact_secrets(score.to_payload())
        self._local_path.parent.mkdir(parents=True, exist_ok=True)
        with self._local_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        provider_statuses: list[TelemetryStatus] = []
        if self._telemetry is not None:
            result = await self._telemetry.publish_record(
                TelemetryRecord(
                    name=f"agent_harness.eval.score.{score.metric}",
                    record_type="metric",
                    context=TelemetryContext(
                        tenant_id=score.tenant_id,
                        agent_id=score.agent_id,
                        run_id=score.run_id,
                        trace_id=score.trace_id,
                        eval_run_id=score.eval_run_id,
                    ),
                    payload=payload,
                ),
                event_type=CanonicalEventType.EVAL_SCORE_RECORDED,
            )
            provider_statuses = result.provider_statuses
        return ScoreSinkResult(
            local_status="written",
            provider_statuses=provider_statuses,
            local_ref=str(self._local_path),
        )
