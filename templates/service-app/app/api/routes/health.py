"""公开只读的 application health/capability 路由。"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request

from agent_harness.config import HarnessSettings
from agent_harness.contracts import ApiErrorEnvelope
from agent_harness.contracts.dto import HarnessDTO
from agent_harness.security.redaction import redact_secrets
from app.api.routes.runs import request_id_from

_PUBLIC_LABEL = re.compile(r"[A-Za-z0-9_.-]{1,64}")


class HealthCapability(HarnessDTO):
    """单个已装配 capability 的公开摘要，不承载连接信息。"""

    kind: str
    status: Literal["configured"] = "configured"


class HealthSummary(HarnessDTO):
    """由类型化 profile 提取、供 route 注入的 allowlist 摘要。"""

    profile: str
    storage: HealthCapability
    queue: HealthCapability
    observability: HealthCapability


class HealthResponse(HealthSummary):
    """HLT-001 返回的 liveness/capability DTO。"""

    request_id: str
    status: Literal["ok", "degraded"]


def _public_label(value: str) -> str:
    """只允许短标识进入公开 health，拒绝路径、URL 和可识别 secret。"""

    redacted = str(redact_secrets(value))
    if redacted != value or _PUBLIC_LABEL.fullmatch(value) is None:
        return "custom"
    return value


def health_summary_from_settings(settings: HarnessSettings) -> HealthSummary:
    """从配置白名单字段构造摘要，绝不序列化 settings/provider 对象。"""

    return HealthSummary(
        profile=_public_label(settings.profile),
        storage=HealthCapability(kind=_public_label(settings.storage.kind)),
        queue=HealthCapability(kind=_public_label(settings.queue.kind)),
        observability=HealthCapability(kind=_public_label(settings.observability.kind)),
    )


def get_health_summary() -> HealthSummary:
    """由唯一 app factory 注入 profile 摘要。"""

    raise RuntimeError("HealthSummary dependency is not configured")


router = APIRouter(prefix="/api/v1", tags=["health"])


@router.get(
    "/health",
    response_model=HealthResponse,
    responses={500: {"model": ApiErrorEnvelope}},
)
async def read_health(
    request: Request,
    summary: Annotated[HealthSummary, Depends(get_health_summary)],
) -> HealthResponse:
    """返回进程内配置装配状态；不做网络探测、写入或认证。"""

    return HealthResponse(
        request_id=request_id_from(request),
        status="ok",
        **summary.to_payload(),
    )
