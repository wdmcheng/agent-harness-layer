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
    """验证 file eval 只运行 approved 样本、跳过 drafts，并按真实输出计分。"""

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
        """将输入值翻倍的确定性执行器，作为成功计分的最小业务替身。"""

        async def execute(self, case: dict[str, Any]) -> dict[str, Any]:
            """从合同 case 读取输入并返回期望输出，避免依赖模型或网络。"""

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
    """验证 executor 失败仍产生失败分数与脱敏本地证据，而不是悄悄丢弃 case。"""

    approved = tmp_path / "dataset" / "approved"
    approved.mkdir(parents=True)
    case: dict[str, Any] = {
        "case_id": "approved-failure",
        "agent_id": "examples.eval",
        "payload": {"input": {"token": "secret-token"}, "expected": {"value": 4}},
    }
    (approved / "approved-failure.json").write_text(json.dumps(case), encoding="utf-8")

    class FailingCaseExecutor:
        """稳定抛出含秘密文本的执行器，用于验证失败计分与脱敏边界。"""

        async def execute(self, case: dict[str, Any]) -> dict[str, Any]:
            """忽略输入并模拟 provider 失败，确保异常路径不回显原始 token。"""

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
    """验证可选 telemetry provider 降级不影响本地成功证据，且错误文本被脱敏。"""

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
        """发送时泄露式失败的观测提供方替身，用于验证 facade 的安全降级。"""

        provider_name = "failing-provider"

        async def send(self, record: TelemetryRecord) -> TelemetryStatus:
            """丢弃记录并抛出含密码文本的异常，测试不得在任何证据中保留该值。"""

            del record
            raise RuntimeError("password=provider-secret")

    class CaseExecutor:
        """返回固定成功状态的执行器，使该场景只覆盖 provider 降级分支。"""

        async def execute(self, case: dict[str, Any]) -> dict[str, Any]:
            """忽略 case 内容并返回 expected 状态，避免额外业务变量干扰断言。"""

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
