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
    """构造覆盖全部行为输入类别的稳定 harness 清单，供 API 夹具共享。"""

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
    """按冻结 split 生成确定性评分的评估器替身。

    baseline 与 candidate 在优化集上故意给出不同分值，使 API 合同能够验证比较结果而
    无需调用真实模型或外部评估服务。
    """

    def __init__(self, baseline_id: str, candidate_id: str) -> None:
        """保存两个版本身份并初始化调用记录，供并发和重放断言使用。"""

        self.baseline_id = baseline_id
        self.candidate_id = candidate_id
        self.calls: list[str] = []

    async def evaluate(self, **kwargs: Any):
        """为冻结 split 的每个 case 生成可追溯、无敏感内容的确定性评估结果。"""

        from agent_harness.evals import ExperimentCaseResult, ExperimentEvaluationResult

        split = kwargs["split"]
        version_id = kwargs["harness_version"].version_id
        self.calls.append(version_id)
        cases: list[ExperimentCaseResult] = []
        # 直接从服务冻结的 case id 构造子集映射，避免夹具另行计算 split 规则。
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
    """携带敏感形态错误文本的可选发布器替身，用于验证发布失败已脱敏且不阻断本地结果。"""

    provider_name = "optional-eval-provider"

    async def publish(self, payload: dict[str, Any]) -> dict[str, object]:
        """拒绝发布请求，模拟 provider 失败而不消费或保存原始 payload。"""

        del payload
        raise RuntimeError("Authorization: Bearer provider-secret-123456")


class UnsafePublisher:
    """返回伪造身份、超大正文和绝对路径的发布器替身，覆盖输出净化边界。"""

    provider_name = "unsafe-provider"

    async def publish(self, payload: dict[str, Any]) -> dict[str, object]:
        """产生故意不安全的 provider 响应，供服务层验证后截断和脱敏。"""

        del payload
        return {
            "provider": "spoofed-provider",
            "status": "completed",
            "detail": "x" * 20_000,
            "raw_response": "provider raw body",
            "evidence_refs": ["/Users/alice/private-provider.json"],
        }


class BlockingEvaluator(SplitAwareEvaluator):
    """首个评估调用会阻塞的评估器替身，用于精确控制执行租约与并发窗口。"""

    def __init__(self, baseline_id: str, candidate_id: str) -> None:
        """在基础确定性评估器上增加开始和释放事件。"""

        super().__init__(baseline_id, candidate_id)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def evaluate(self, **kwargs: Any):
        """仅阻塞第一次评估，以暴露同一实验的并发 claim 行为。"""

        if not self.calls:
            self.started.set()
            await self.release.wait()
        return await super().evaluate(**kwargs)


class ExplodingExperimentService:
    """总是失败并含敏感文本的实验服务替身，用于验证 API 最终错误信封。"""

    def __init__(self) -> None:
        """记录调用次数，证明路由不会在失败后隐式重试创建副作用。"""

        self.calls = 0

    async def create(self, _request: object) -> object:
        """模拟内部服务异常，检验模板错误处理不会泄露 provider 凭据。"""

        self.calls += 1
        raise RuntimeError("Authorization: Bearer internal-provider-secret")


async def seed_approved_cases(storage: Any) -> None:
    """写入三个已批准且带行为标签的 case，作为实验 API 的最小冻结数据集。"""

    from agent_harness.storage import EvalCaseCreate

    async with storage.uow() as uow:
        await uow.tenants.ensure("tenant-a")
        for index in range(3):
            # 每个 case 具有独立期望输出，保证 split 和比较都基于真实持久化记录。
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
    """构造创建实验的有效 API 请求体，并返回 candidate 版本以便断言响应。"""

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
