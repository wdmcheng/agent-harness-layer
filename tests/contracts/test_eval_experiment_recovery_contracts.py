"""Experiment 并发冲突、执行中断与 claim fencing 恢复合同。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from tests.contracts.auth_policy_hitl_contract_helpers import sqlite_dsn, table_count
from tests.contracts.test_eval_experiment_api_contracts import (
    BlockingEvaluator,
    SplitAwareEvaluator,
)


def experiment_body() -> tuple[dict[str, object], str]:
    """构造可比较的 baseline/candidate harness 请求体。

    同时返回候选版本标识，供 evaluator 夹具与目标版本建立稳定绑定。
    """

    from agent_harness.evals import HarnessInputSource, HarnessVersionBuilder

    builder = HarnessVersionBuilder()

    def manifest(seed: str):
        """从固定输入维度构建一个脱敏 harness 版本，seed 仅改变版本内容以制造比较双方。"""

        return builder.build(
            {
                "prompt_instruction": HarnessInputSource(value={"prompt": seed}),
                "tool_descriptions": HarnessInputSource(value=[]),
                "agent_config": HarnessInputSource(value={"max_steps": 4}),
                "retrieval_config": HarnessInputSource(value={"top_k": 5}),
                "policy_defaults": HarnessInputSource(value={"network": "deny"}),
                "model_adapter_settings": HarnessInputSource(value={"adapter": "fake"}),
            }
        )

    baseline = manifest("baseline")
    candidate = manifest("candidate")
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


async def seed_approved_cases(storage: Any) -> None:
    """写入并批准最小多标签评测集，确保实验恢复用例不会被草稿过滤逻辑干扰。"""

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


def experiment_request(*, key: str, tags: list[str] | None = None):
    """基于共享完整请求体生成带稳定幂等键的创建 DTO，可选覆盖目标标签以测试冲突。"""

    from agent_harness.evals import ExperimentCreateRequest

    body, _candidate_id = experiment_body()
    return ExperimentCreateRequest.model_validate(
        {
            **body,
            **({} if tags is None else {"tags": tags}),
            "request_id": f"request-{key}",
            "tenant_id": "tenant-a",
            "idempotency_key": key,
        }
    )


__all__ = [
    "Any",
    "BlockingEvaluator",
    "Path",
    "SplitAwareEvaluator",
    "UTC",
    "asyncio",
    "cast",
    "datetime",
    "experiment_body",
    "experiment_request",
    "pytest",
    "seed_approved_cases",
    "sqlite_dsn",
    "table_count",
]
