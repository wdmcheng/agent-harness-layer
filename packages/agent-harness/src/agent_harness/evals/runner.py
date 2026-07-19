"""已批准数据集的评测执行器，明确排除草稿并保存评分证据。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, cast
from uuid import uuid4

from pydantic import Field

from agent_harness.contracts.dto import HarnessDTO
from agent_harness.evals.review_queue import ReviewDatasetAdapter
from agent_harness.evals.score_sink import ScoreSink
from agent_harness.security.redaction import redact_secrets
from agent_harness.storage import EvalRunCreate, EvalScoreCreate


class EvalRunResult(HarnessDTO):
    """eval runner 对 CLI/API 可见的执行摘要。"""

    eval_run_id: str
    tenant_id: str
    agent_id: str
    dataset: str
    status: str
    case_count: int
    score_summary: dict[str, Any] = Field(default_factory=dict)
    provider_statuses: list[Any] = Field(default_factory=list)
    local_refs: list[str] = Field(default_factory=list)
    skipped_drafts: int = 0


class ApprovedCaseExecutor(Protocol):
    """可选 adapter：把 approved file case 交给真实 agent seam。"""

    async def execute(self, case: dict[str, Any]) -> dict[str, Any]:
        """通过真实 Agent 接缝执行一个已批准案例，并返回可比较的输出投影。"""
        ...


class EvalRunner:
    """只消费 approved case，draft 永远不参与评分。"""

    def __init__(self, *, service: Any | None = None, score_sink: ScoreSink) -> None:
        """装配可选仓储服务和必需评分接收器，兼容 API 与纯文件评测入口。"""
        self._service = service
        self._score_sink = score_sink

    async def run_approved(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        dataset: str = "default",
    ) -> EvalRunResult:
        """从 service repository 读取 approved cases 并写 score evidence。"""

        if self._service is None:
            raise RuntimeError("EvalRunner service is required for repository-backed runs")
        cases = await self._service.list_cases(
            tenant_id=tenant_id,
            status="approved",
            dataset=dataset,
            agent_id=agent_id,
        )
        if not cases:
            run = await self._service.create_eval_run(
                EvalRunCreate(
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    dataset=dataset,
                    status="no_approved_cases",
                    case_count=0,
                    score_summary={"case_count": 0},
                )
            )
            return EvalRunResult(
                eval_run_id=run.eval_run_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
                dataset=dataset,
                status=run.status,
                case_count=0,
                score_summary=run.score_summary,
            )

        scored = [_score_case(case.to_payload()) for case in cases]
        summary = _score_summary(scored)
        run = await self._service.create_eval_run(
            EvalRunCreate(
                tenant_id=tenant_id,
                agent_id=agent_id,
                dataset=dataset,
                status="completed",
                case_count=len(scored),
                score_summary=summary,
            )
        )
        provider_statuses: list[Any] = []
        provider_status_payloads: list[dict[str, object]] = []
        local_refs: list[str] = []
        for case, scored_case in zip(cases, scored, strict=True):
            score = EvalScoreCreate(
                tenant_id=tenant_id,
                eval_run_id=run.eval_run_id,
                case_id=case.case_id,
                agent_id=case.agent_id,
                run_id=case.run_id,
                trace_id=case.trace_id,
                metric="exact_match",
                value=float(scored_case["value"]),
                label=str(scored_case["label"]),
                explanation=str(scored_case["explanation"]),
            )
            sink_result = await self._score_sink.write_score(score)
            provider_statuses.extend(sink_result.provider_statuses)
            if sink_result.local_ref is not None and sink_result.local_ref not in local_refs:
                local_refs.append(sink_result.local_ref)
            score_provider_statuses = [
                status.to_payload() if hasattr(status, "to_payload") else dict(status)
                for status in sink_result.provider_statuses
            ]
            provider_status_payloads.extend(score_provider_statuses)
            await self._service.create_score(
                score.model_copy(
                    update={
                        "provider_ref": sink_result.local_ref,
                        "provider_statuses": score_provider_statuses,
                    }
                )
            )
        final_summary = {**summary, "local_refs": local_refs}
        await self._service.update_eval_run_evidence(
            eval_run_id=run.eval_run_id,
            score_summary=final_summary,
            provider_statuses=provider_status_payloads,
        )
        return EvalRunResult(
            eval_run_id=run.eval_run_id,
            tenant_id=tenant_id,
            agent_id=agent_id,
            dataset=dataset,
            status="completed",
            case_count=len(scored),
            score_summary=final_summary,
            provider_statuses=provider_statuses,
            local_refs=local_refs,
        )

    async def run_file_dataset(
        self,
        *,
        dataset_dir: Path,
        tenant_id: str = "default",
        agent_id: str,
        dataset: str = "default",
        case_executor: ApprovedCaseExecutor | None = None,
    ) -> EvalRunResult:
        """CLI/Makefile 路径：只读 approved 目录，不需要 DB 或真实 provider。"""

        adapter = ReviewDatasetAdapter(
            drafts_dir=dataset_dir / "drafts",
            approved_dir=dataset_dir / "approved",
        )
        cases = adapter.load_approved(agent_id=agent_id)
        skipped_drafts = adapter.count_drafts(agent_id=agent_id)
        eval_run_id = str(uuid4())
        if not cases:
            return EvalRunResult(
                eval_run_id=eval_run_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
                dataset=dataset,
                status="no_approved_cases",
                case_count=0,
                score_summary={"case_count": 0},
                skipped_drafts=skipped_drafts,
            )
        executed_cases: list[dict[str, Any]] = []
        for case in cases:
            if case_executor is None:
                executed_cases.append(case)
                continue
            try:
                output = await case_executor.execute(case)
            except Exception as exc:  # noqa: BLE001 - 失败 case 需要计分，不能丢弃
                output = {"error": str(redact_secrets(str(exc)))}
            executed_cases.append(_case_with_output(case, output))
        scored = [_score_case(case) for case in executed_cases]
        summary = _score_summary(scored)
        local_refs: list[str] = []
        provider_statuses: list[Any] = []
        provider_status_payloads: list[dict[str, object]] = []
        for case, scored_case in zip(cases, scored, strict=True):
            case_id = str(case.get("case_id") or case.get("id") or uuid4())
            sink_result = await self._score_sink.write_score(
                EvalScoreCreate(
                    tenant_id=str(case.get("tenant_id") or tenant_id),
                    eval_run_id=eval_run_id,
                    case_id=case_id,
                    agent_id=str(case.get("agent_id") or agent_id),
                    run_id=_optional_str(case.get("run_id")),
                    trace_id=_optional_str(case.get("trace_id")),
                    metric="exact_match",
                    value=float(scored_case["value"]),
                    label=str(scored_case["label"]),
                    explanation=str(scored_case["explanation"]),
                )
            )
            provider_statuses.extend(sink_result.provider_statuses)
            provider_status_payloads.extend(
                status.to_payload() for status in sink_result.provider_statuses
            )
            if sink_result.local_ref is not None and sink_result.local_ref not in local_refs:
                local_refs.append(sink_result.local_ref)
        final_summary = {
            **summary,
            "local_refs": local_refs,
            "provider_statuses": provider_status_payloads,
        }
        return EvalRunResult(
            eval_run_id=eval_run_id,
            tenant_id=tenant_id,
            agent_id=agent_id,
            dataset=dataset,
            status="completed",
            case_count=len(scored),
            score_summary=final_summary,
            provider_statuses=provider_statuses,
            local_refs=local_refs,
            skipped_drafts=skipped_drafts,
        )


def _score_case(case: dict[str, Any]) -> dict[str, Any]:
    """按 fixture 的 expected/output 做确定性精确匹配评分。

    缺少 expected 表示案例只验证可执行性，按通过处理；这不是通用语义评分器，
    更复杂指标必须通过独立 metric 版本接入，避免在基础 runner 中改变历史分数。
    """
    raw_payload = case.get("payload")
    payload = cast(dict[str, Any], raw_payload) if isinstance(raw_payload, dict) else case
    expected = payload.get("expected")
    output = payload.get("output")
    value = 1.0 if expected is None or expected == output else 0.0
    return {
        "value": value,
        "label": "passed" if value == 1.0 else "failed",
        "explanation": "expected output matched" if value == 1.0 else "expected output differed",
    }


def _score_summary(scores: list[dict[str, Any]]) -> dict[str, Any]:
    """汇总案例数量、通过失败数和平均分；空集合明确返回零均分。"""
    total = len(scores)
    passed = sum(1 for score in scores if score["value"] == 1.0)
    return {
        "case_count": total,
        "passed": passed,
        "failed": total - passed,
        "mean_score": 0.0 if total == 0 else passed / total,
    }


def _optional_str(value: object) -> str | None:
    """将可选外部标识规范化为字符串，保留空值以避免生成伪造引用。"""
    return None if value is None else str(value)


def _case_with_output(case: dict[str, Any], output: dict[str, Any]) -> dict[str, Any]:
    """复制案例并写入执行输出，绝不原地修改已批准文件解析出的原始 fixture。"""
    copied = dict(case)
    raw_payload = copied.get("payload")
    if isinstance(raw_payload, dict):
        copied["payload"] = {**cast(dict[str, Any], raw_payload), "output": output}
    else:
        copied["output"] = output
    return copied
