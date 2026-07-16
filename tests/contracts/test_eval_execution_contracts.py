"""File eval executor、draft 隔离与降级证据合同测试。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent_harness.evals import EvalRunner, ScoreSink
from agent_harness.events import LocalJsonlEventSink
from agent_harness.observability import TelemetryFacade, TelemetryRecord, TelemetryStatus


@pytest.mark.asyncio
async def test_file_eval_optional_executor_preserves_drafts_and_scores_real_output(
    tmp_path: Path,
) -> None:
    approved = tmp_path / "dataset" / "approved"
    drafts = tmp_path / "dataset" / "drafts"
    approved.mkdir(parents=True)
    drafts.mkdir(parents=True)
    case = {
        "case_id": "approved-1",
        "agent_id": "examples.eval",
        "payload": {"input": {"value": 2}, "expected": {"value": 4}},
    }
    draft: dict[str, Any] = {
        "case_id": "draft-1",
        "agent_id": "examples.eval",
        "payload": {},
    }
    (approved / "approved-1.json").write_text(json.dumps(case), encoding="utf-8")
    (drafts / "draft-1.json").write_text(json.dumps(draft), encoding="utf-8")

    class CaseExecutor:
        async def execute(self, case: dict[str, Any]) -> dict[str, Any]:
            payload = case["payload"]
            return {"value": int(payload["input"]["value"]) * 2}

    runner = EvalRunner(score_sink=ScoreSink(local_path=tmp_path / "scores.jsonl"))
    result = await runner.run_file_dataset(
        dataset_dir=tmp_path / "dataset",
        agent_id="examples.eval",
        case_executor=CaseExecutor(),
    )

    assert result.status == "completed"
    assert result.case_count == 1
    assert result.skipped_drafts == 1
    assert result.score_summary["passed"] == 1


@pytest.mark.asyncio
async def test_file_eval_executor_failure_is_scored_instead_of_dropped(tmp_path: Path) -> None:
    approved = tmp_path / "dataset" / "approved"
    approved.mkdir(parents=True)
    case: dict[str, Any] = {
        "case_id": "approved-failure",
        "agent_id": "examples.eval",
        "payload": {"input": {"token": "secret-token"}, "expected": {"value": 4}},
    }
    (approved / "approved-failure.json").write_text(json.dumps(case), encoding="utf-8")

    class FailingCaseExecutor:
        async def execute(self, case: dict[str, Any]) -> dict[str, Any]:
            del case
            raise RuntimeError("provider failed token=secret-token")

    scores = tmp_path / "scores.jsonl"
    runner = EvalRunner(score_sink=ScoreSink(local_path=scores))
    result = await runner.run_file_dataset(
        dataset_dir=tmp_path / "dataset",
        agent_id="examples.eval",
        case_executor=FailingCaseExecutor(),
    )

    assert result.case_count == 1
    assert result.score_summary["failed"] == 1
    assert "secret-token" not in scores.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_file_eval_provider_degradation_keeps_redacted_local_evidence(tmp_path: Path) -> None:
    approved = tmp_path / "dataset" / "approved"
    approved.mkdir(parents=True)
    case: dict[str, Any] = {
        "case_id": "approved-provider-degrade",
        "agent_id": "examples.eval",
        "payload": {"expected": {"status": "ok"}},
    }
    (approved / "approved-provider-degrade.json").write_text(
        json.dumps(case),
        encoding="utf-8",
    )

    class FailingProvider:
        provider_name = "failing-provider"

        async def send(self, record: TelemetryRecord) -> TelemetryStatus:
            del record
            raise RuntimeError("password=provider-secret")

    class CaseExecutor:
        async def execute(self, case: dict[str, Any]) -> dict[str, Any]:
            del case
            return {"status": "ok"}

    scores = tmp_path / "scores.jsonl"
    traces = tmp_path / "traces.jsonl"
    runner = EvalRunner(
        score_sink=ScoreSink(
            local_path=scores,
            telemetry=TelemetryFacade(
                local_sink=LocalJsonlEventSink(traces),
                providers=[FailingProvider()],
            ),
        )
    )
    result = await runner.run_file_dataset(
        dataset_dir=tmp_path / "dataset",
        agent_id="examples.eval",
        case_executor=CaseExecutor(),
    )

    assert result.score_summary["passed"] == 1
    assert len(result.provider_statuses) == 1
    provider_status = result.provider_statuses[0]
    assert provider_status.status == "degraded"
    assert provider_status.detail is not None
    assert "provider-secret" not in provider_status.detail
    evidence = scores.read_text(encoding="utf-8") + traces.read_text(encoding="utf-8")
    assert "provider-secret" not in evidence
