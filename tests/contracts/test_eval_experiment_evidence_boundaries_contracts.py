"""Experiment evidence 派生边界与真实 create split 合同。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError
from tests.contracts.auth_policy_hitl_contract_helpers import sqlite_dsn
from tests.contracts.test_eval_experiment_api_contracts import SplitAwareEvaluator
from tests.contracts.test_eval_experiment_recovery_contracts import (
    experiment_request,
    seed_approved_cases,
)


class BoundaryRefsEvaluator(SplitAwareEvaluator):
    """按版本注入指定本地证据引用的评估器替身，用于测试证据边界规则。"""

    def __init__(
        self,
        baseline_id: str,
        candidate_id: str,
        refs_by_version: dict[str, list[str]],
    ) -> None:
        """保存版本到证据引用的映射，其他评分逻辑继承基础确定性夹具。"""

        super().__init__(baseline_id, candidate_id)
        self.refs_by_version = refs_by_version

    async def evaluate(self, **kwargs: Any):
        """生成基础结果后替换本地证据引用，覆盖服务端净化前的输入形状。"""

        result = await super().evaluate(**kwargs)
        result.local_evidence_refs = list(self.refs_by_version[result.harness_version_id])
        return result


class CapturingPublisher:
    """记录净化后外发 payload 的发布器替身，不向外部网络发送任何数据。"""

    provider_name = "bounded-evidence-test"

    def __init__(self) -> None:
        """初始化按调用顺序保存的 payload 列表。"""

        self.payloads: list[dict[str, Any]] = []

    async def publish(self, payload: dict[str, Any]) -> dict[str, object]:
        """保存收到的 payload 并返回最小成功结果，供测试检查外发边界。"""

        self.payloads.append(payload)
        return {"status": "completed", "evidence_refs": []}


class BoundaryCaseRefsEvaluator(SplitAwareEvaluator):
    """为单个 case 注入可控数量和长度的证据引用，验证集合与单项上限。"""

    def __init__(
        self,
        baseline_id: str,
        candidate_id: str,
        *,
        ref_count: int,
        ref_padding: int,
    ) -> None:
        """保存证据引用数量和填充长度，使边界测试不依赖随机文本。"""

        super().__init__(baseline_id, candidate_id)
        self.ref_count = ref_count
        self.ref_padding = ref_padding

    async def evaluate(self, **kwargs: Any):
        """将首个 case 改为确定性失败结果并填充指定证据引用集合。"""

        result = await super().evaluate(**kwargs)
        item = result.case_results[0]
        # 让 baseline 通过、candidate 失败，确保比较结果保留被截断的 case 证据路径。
        baseline = result.harness_version_id == self.baseline_id
        item.metric_scores = {"exact_match": 1.0 if baseline else 0.0}
        item.passed = baseline
        side = "baseline" if baseline else "candidate"
        item.evidence_refs = [
            f"artifact://bounded-case/{side}/{index}/" + "x" * self.ref_padding
            for index in range(self.ref_count)
        ]
        return result


class InvalidPartialEvaluator:
    """抛出携带非法部分结果的评估器替身，验证服务不会保存不属于冻结 split 的 case。"""

    async def evaluate(self, **kwargs: Any):
        """构造不在 split 内的 partial result 后失败，模拟 provider 的不可信恢复载荷。"""

        from agent_harness.evals import (
            ExperimentCaseResult,
            ExperimentEvaluationFailure,
            ExperimentEvaluationResult,
        )

        version_id = kwargs["harness_version"].version_id
        partial = ExperimentEvaluationResult(
            harness_version_id=version_id,
            evaluator_profile=kwargs["evaluator_profile"],
            metric_versions=kwargs["metric_versions"],
            case_results=[
                ExperimentCaseResult(
                    case_id="not-in-frozen-split",
                    subset="holdout",
                    tags=["tool_selection"],
                    metric_scores={"exact_match": 0.0},
                    passed=False,
                )
            ],
        )
        raise ExperimentEvaluationFailure("partial failure", partial_result=partial)


class LargeRawErrorEvaluator:
    """抛出超大原始异常文本的评估器替身，验证错误持久化和 API 输出都有上限。"""

    async def evaluate(self, **_kwargs: Any):
        """产生百万字符 provider 错误而不附带可复用部分结果。"""

        raise RuntimeError("provider raw response:" + "x" * 1_000_000)


class UnsafeSuccessfulEvaluator(SplitAwareEvaluator):
    """在成功评估结果中注入密钥、超长引用或绝对路径，覆盖成功路径净化。"""

    def __init__(self, baseline_id: str, candidate_id: str, unsafe_mode: str) -> None:
        """保存不安全输入模式，基础评分和 split 处理仍交给父类。"""

        super().__init__(baseline_id, candidate_id)
        self.unsafe_mode = unsafe_mode

    async def evaluate(self, **kwargs: Any):
        """返回带指定不安全证据的成功结果，交由服务边界决定拒绝或截断。"""

        result = await super().evaluate(**kwargs)
        if self.unsafe_mode == "secret":
            result.case_results[0].evidence_refs = ["api_key=successful-evaluator-secret-123456"]
        elif self.unsafe_mode == "oversized":
            result.case_results[0].evidence_refs = ["x" * 1_000_000]
        elif self.unsafe_mode == "oversized_list":
            result.case_results[0].evidence_refs = [
                f"artifact://mutated/{index}/" + "x" * 150 for index in range(100)
            ]
        else:
            result.local_evidence_refs = ["/Users/alice/private-evaluator-result.json"]
        return result


__all__ = [
    "Any",
    "BoundaryCaseRefsEvaluator",
    "BoundaryRefsEvaluator",
    "CapturingPublisher",
    "InvalidPartialEvaluator",
    "LargeRawErrorEvaluator",
    "Path",
    "SplitAwareEvaluator",
    "UnsafeSuccessfulEvaluator",
    "ValidationError",
    "cast",
    "experiment_request",
    "json",
    "pytest",
    "seed_approved_cases",
    "sqlite_dsn",
]
