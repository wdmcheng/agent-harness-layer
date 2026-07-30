"""真实模型增量 smoke 的去敏结果 schema 与失败归因。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from agent_harness.models import ModelProviderInvocationError

SCHEMA_VERSION = "model-stream-live-smoke/v1"
AUTHORIZED_ENV = "AGENT_HARNESS_LIVE_MODEL_AUTHORIZED"
STREAM_OPT_IN_ENV = "AGENT_HARNESS_LIVE_MODEL_STREAM_OPT_IN"

type SmokeResult = dict[str, object]

_FIELDS = {
    "schema_version",
    "status",
    "provider_called",
    "existing_event_first_frame_ms",
    "provider_first_delta_ms",
    "committed_first_delta_ms",
    "client_first_delta_ms",
    "reason_code",
}
_HOSTED_REASONS = {
    "authorization_missing",
    "stream_opt_in_missing",
    "credential_missing",
    "endpoint_untrusted",
}
_EXTERNAL_REASONS = {
    "network_unavailable",
    "provider_rejected",
    "quota_blocked",
    "provider_timeout",
    "provider_result_unknown",
}
_LOCAL_REASONS = {"contract_failure"}


def make_result(
    *,
    status: str,
    provider_called: bool,
    existing_event_first_frame_ms: int | None = None,
    provider_first_delta_ms: int | None = None,
    committed_first_delta_ms: int | None = None,
    client_first_delta_ms: int | None = None,
    reason_code: str | None,
) -> SmokeResult:
    """构造不含 prompt、文本、endpoint、header、identity 或异常的安全 artifact。"""

    return validate_result(
        {
            "schema_version": SCHEMA_VERSION,
            "status": status,
            "provider_called": provider_called,
            "existing_event_first_frame_ms": existing_event_first_frame_ms,
            "provider_first_delta_ms": provider_first_delta_ms,
            "committed_first_delta_ms": committed_first_delta_ms,
            "client_first_delta_ms": client_first_delta_ms,
            "reason_code": reason_code,
        }
    )


def validate_result(payload: Mapping[str, object]) -> SmokeResult:
    """逐字段验证机器证据，防止状态漂移或内容字段进入 CI artifact。"""

    if set(payload) != _FIELDS or payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("stream live smoke schema is invalid")
    status = payload.get("status")
    provider_called = payload.get("provider_called")
    reason = payload.get("reason_code")
    timing_names = (
        "existing_event_first_frame_ms",
        "provider_first_delta_ms",
        "committed_first_delta_ms",
        "client_first_delta_ms",
    )
    timings = [payload.get(name) for name in timing_names]
    if status not in {"passed", "failed", "hosted-unverified", "external-blocked"}:
        raise ValueError("stream live smoke status is invalid")
    if not isinstance(provider_called, bool):
        raise ValueError("stream live smoke provider_called must be boolean")
    if any(
        value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0)
        for value in timings
    ):
        raise ValueError("stream live smoke timings must be non-negative integers or null")
    existing, provider, committed, client = cast(
        tuple[int | None, int | None, int | None, int | None],
        tuple(timings),
    )
    if existing is not None and existing >= 1000:
        raise ValueError("existing committed event first frame exceeds the frozen bound")
    observed = [value for value in (provider, committed, client) if value is not None]
    if observed != sorted(observed):
        raise ValueError("stream live smoke timing order is invalid")
    if status == "passed":
        if (
            provider_called is not True
            or reason is not None
            or any(value is None for value in timings)
        ):
            raise ValueError("passed stream smoke requires all timing evidence")
    elif status == "hosted-unverified":
        if (
            provider_called
            or reason not in _HOSTED_REASONS
            or any(value is not None for value in timings)
        ):
            raise ValueError("hosted-unverified stream smoke is inconsistent")
    elif status == "failed":
        if reason not in _LOCAL_REASONS:
            raise ValueError("failed stream smoke reason is invalid")
    elif reason not in _EXTERNAL_REASONS:
        raise ValueError("external-blocked stream smoke reason is invalid")
    return dict(payload)


def _external_reason(error: ModelProviderInvocationError | None) -> str:
    """从 invocation 的稳定错误身份映射封闭外部阻断原因。"""

    if error is not None and error.code == "model.provider_side_effect_unknown":
        return "provider_result_unknown"
    return "provider_rejected"


def classify_incomplete_run(
    *,
    response_observed: bool,
    error: ModelProviderInvocationError | None,
    existing_event_first_frame_ms: int | None,
    provider_first_delta_ms: int | None,
    committed_first_delta_ms: int | None,
    client_first_delta_ms: int | None,
) -> tuple[SmokeResult, int]:
    """区分 provider 外部阻断与本地编排失败，并保留已观察的调用事实。"""

    is_local_failure = response_observed or error is None or error.failure_domain == "runtime"
    if is_local_failure:
        provider_called = (
            response_observed
            or provider_first_delta_ms is not None
            or bool(error.provider_called if error is not None else False)
        )
        return make_result(
            status="failed",
            provider_called=provider_called,
            existing_event_first_frame_ms=existing_event_first_frame_ms,
            provider_first_delta_ms=provider_first_delta_ms,
            committed_first_delta_ms=committed_first_delta_ms,
            client_first_delta_ms=client_first_delta_ms,
            reason_code="contract_failure",
        ), 1
    assert error is not None
    return make_result(
        status="external-blocked",
        provider_called=bool(error.provider_called),
        existing_event_first_frame_ms=existing_event_first_frame_ms,
        provider_first_delta_ms=provider_first_delta_ms,
        committed_first_delta_ms=committed_first_delta_ms,
        client_first_delta_ms=client_first_delta_ms,
        reason_code=_external_reason(error),
    ), 2


__all__ = [
    "AUTHORIZED_ENV",
    "SCHEMA_VERSION",
    "STREAM_OPT_IN_ENV",
    "SmokeResult",
    "classify_incomplete_run",
    "make_result",
    "validate_result",
]
