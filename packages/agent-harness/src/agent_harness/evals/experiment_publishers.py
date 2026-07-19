"""可选 evidence provider 的封闭状态映射与安全降级。"""

from __future__ import annotations

import re
from typing import Any, cast

from agent_harness.evals.experiment_models import (
    ExperimentComparison,
    ExperimentEvidencePublisher,
    ExperimentProviderStatus,
)
from agent_harness.storage import EvalExperimentRecord


async def publish_experiment_evidence(
    *,
    publishers: list[ExperimentEvidencePublisher],
    record: EvalExperimentRecord,
    comparison: ExperimentComparison | None,
) -> list[dict[str, object]]:
    """向可选证据提供方扇出实验摘要，并把单个失败降级为可审计状态。

    本地持久化已经完成才会进入该函数，因此任何 provider 异常都不得反向
    影响实验主结果；返回值仅保留经过类型和名称约束的公开状态。
    """

    payload = {
        "experiment_id": record.experiment_id,
        "status": record.status,
        "comparison": None if comparison is None else comparison.to_payload(),
        "local_evidence_refs": record.local_refs,
    }
    statuses: list[dict[str, object]] = []
    for publisher in publishers:
        try:
            raw_status = await publisher.publish(payload)
            detail = raw_status.get("detail")
            evidence_refs = raw_status.get("evidence_refs", [])
            typed_refs = (
                cast(list[object], evidence_refs) if isinstance(evidence_refs, list) else []
            )
            status = ExperimentProviderStatus(
                provider=_safe_provider_name(publisher.provider_name),
                status=cast(Any, raw_status.get("status", "completed")),
                detail=detail if isinstance(detail, str) else None,
                evidence_refs=[ref for ref in typed_refs if isinstance(ref, str)],
            )
        except Exception:  # noqa: BLE001 - optional provider must degrade safely
            status = ExperimentProviderStatus(
                provider=_safe_provider_name(publisher.provider_name),
                status="degraded",
                detail="provider publish failed or returned an invalid status",
            )
        statuses.append(cast(dict[str, object], status.to_payload()))
    return statuses


def _safe_provider_name(value: str) -> str:
    """限制第三方名称的字符集和长度，防止异常内容污染证据与展示字段。"""

    return value if re.fullmatch(r"[A-Za-z0-9._-]{1,100}", value) else "invalid-provider"
