"""真实多 deployment failover smoke 的去敏判别联合与证据校验。"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import cast

SCHEMA_VERSION = "model-failover-live-smoke/v1"

type SmokeResult = dict[str, object]

_TOP_FIELDS = {
    "schema_version",
    "status",
    "provider_called",
    "attempt_count",
    "chain_id",
    "selected_ordinal",
    "candidates",
    "usage",
    "reason_code",
}
_CANDIDATE_FIELDS = {
    "ordinal",
    "deployment_id",
    "provider",
    "model",
    "outcome",
    "attempt_count",
    "not_started_proof_count",
    "request_sent",
    "response_observed",
    "not_started_reason",
    "http_status",
}
_USAGE_FIELDS = {"input_tokens", "output_tokens", "cost_usd", "cost_status"}
_HOSTED_REASONS = {
    "authorization_missing",
    "failover_opt_in_missing",
    "credential_pair_missing",
    "deployment_pair_invalid",
    "not_started_fixture_missing",
}
_EXTERNAL_REASONS = {
    "network_unavailable",
    "provider_rejected",
    "quota_blocked",
    "provider_timeout",
    "provider_result_unknown",
}


def _empty_result(*, status: str, reason_code: str) -> SmokeResult:
    """构造 chain 冻结前唯一允许的零调用形状。"""

    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "provider_called": False,
        "attempt_count": 0,
        "chain_id": None,
        "selected_ordinal": None,
        "candidates": [],
        "usage": None,
        "reason_code": reason_code,
    }


def preflight_result(
    *,
    authorized: bool = False,
    failover_opt_in: bool = False,
    credential_pair_present: bool = False,
    deployment_pair_valid: bool = False,
    not_started_fixture_present: bool = False,
) -> SmokeResult:
    """按冻结优先级返回零调用结果；此函数不读取任何 credential 内容。"""

    checks = (
        (authorized, "authorization_missing"),
        (failover_opt_in, "failover_opt_in_missing"),
        (credential_pair_present, "credential_pair_missing"),
        (deployment_pair_valid, "deployment_pair_invalid"),
        (not_started_fixture_present, "not_started_fixture_missing"),
    )
    for satisfied, reason in checks:
        if not satisfied:
            return _empty_result(status="hosted-unverified", reason_code=reason)
    raise ValueError("complete failover preflight requires the live execution producer")


def validate_preflight_routes(routes: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    """冻结恰好两个隔离 deployment/credential/endpoint，provider kind 可相同。"""

    expected = {
        "deployment_id",
        "provider_kind",
        "credential_ref",
        "endpoint_origin",
        "max_attempts",
    }
    if len(routes) != 2:
        raise ValueError("failover live smoke requires exactly two routes")
    normalized: list[dict[str, object]] = []
    for route in routes:
        if set(route) != expected:
            raise ValueError("failover preflight route shape is invalid")
        for field in ("deployment_id", "provider_kind", "credential_ref", "endpoint_origin"):
            if not isinstance(route.get(field), str) or not route[field]:
                raise ValueError("failover preflight route identity is invalid")
        if route.get("max_attempts") != 1:
            raise ValueError("failover smoke routes must freeze max_attempts=1")
        origin = cast(str, route["endpoint_origin"])
        if not origin.startswith("https://") or origin.endswith("/"):
            raise ValueError("failover preflight endpoint origin is invalid")
        normalized.append(dict(route))
    for field in ("deployment_id", "credential_ref", "endpoint_origin"):
        if normalized[0][field] == normalized[1][field]:
            raise ValueError(f"failover preflight routes reuse {field}")
    return normalized


def _non_bool_int(value: object, *, minimum: int = 0) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value >= minimum


def _typed_mapping(value: Mapping[object, object]) -> dict[str, object]:
    """把运行时 Mapping 收窄为 string-keyed JSON object。"""

    if any(not isinstance(key, str) for key in value):
        raise ValueError("failover live object keys must be strings")
    return {cast(str, key): item for key, item in value.items()}


def _validate_usage(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(cast(Mapping[object, object], value)) != _USAGE_FIELDS:
        raise ValueError("failover live usage shape is invalid")
    usage = _typed_mapping(cast(Mapping[object, object], value))
    if not _non_bool_int(usage["input_tokens"]) or not _non_bool_int(usage["output_tokens"]):
        raise ValueError("failover live usage tokens are invalid")
    status = usage["cost_status"]
    cost = usage["cost_usd"]
    if status not in {"reported", "estimated", "unavailable"}:
        raise ValueError("failover live usage cost status is invalid")
    if cost is not None and (
        isinstance(cost, bool)
        or not isinstance(cost, int | float)
        or not math.isfinite(cost)
        or cost < 0
    ):
        raise ValueError("failover live usage cost is invalid")
    if (cost is None) != (status == "unavailable"):
        raise ValueError("failover live usage cost union is invalid")
    return usage


def _validate_candidates(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise ValueError("failover live candidates must be a list")
    candidates: list[dict[str, object]] = []
    for expected_ordinal, raw in enumerate(cast(list[object], value), start=1):
        if (
            not isinstance(raw, Mapping)
            or set(cast(Mapping[object, object], raw)) != _CANDIDATE_FIELDS
        ):
            raise ValueError("failover live candidate shape is invalid")
        item = _typed_mapping(cast(Mapping[object, object], raw))
        if item["ordinal"] != expected_ordinal:
            raise ValueError("failover live candidate ordinals are invalid")
        for field in ("deployment_id", "provider", "model"):
            if not isinstance(item[field], str) or not item[field]:
                raise ValueError("failover live candidate identity is invalid")
        if not _non_bool_int(item["attempt_count"]) or not _non_bool_int(
            item["not_started_proof_count"]
        ):
            raise ValueError("failover live candidate counts are invalid")
        if not isinstance(item["request_sent"], bool) or not isinstance(
            item["response_observed"], bool
        ):
            raise ValueError("failover live candidate observation is invalid")
        if item["response_observed"] and not item["request_sent"]:
            raise ValueError("response observation requires a sent request")
        status = item["http_status"]
        if status is not None and not _non_bool_int(status, minimum=100):
            raise ValueError("failover live candidate HTTP status is invalid")
        if isinstance(status, int) and status > 599:
            raise ValueError("failover live candidate HTTP status is invalid")
        outcome = item["outcome"]
        reason = item["not_started_reason"]
        attempts = cast(int, item["attempt_count"])
        proofs = cast(int, item["not_started_proof_count"])
        if outcome not in {"not_started", "completed", "unknown", "not_called"}:
            raise ValueError("failover live candidate outcome is invalid")
        if outcome == "not_called":
            if attempts or proofs or item["request_sent"] or item["response_observed"]:
                raise ValueError("not-called candidate contains invocation facts")
            if reason is not None or status is not None:
                raise ValueError("not-called candidate contains result facts")
        elif outcome == "not_started":
            if attempts < 1 or proofs != attempts:
                raise ValueError("not-started candidate proof count is invalid")
            if reason == "client_not_started":
                if item["request_sent"] or item["response_observed"] or status is not None:
                    raise ValueError("client-not-started facts are invalid")
            elif reason == "trusted_business_not_started":
                if (
                    not item["request_sent"]
                    or not item["response_observed"]
                    or status not in {403, 429, *range(500, 600)}
                ):
                    raise ValueError("trusted-business-not-started facts are invalid")
            else:
                raise ValueError("not-started reason is invalid")
        elif proofs != 0 or reason is not None:
            raise ValueError("non-proof candidate carries a proof")
        candidates.append(item)
    return candidates


def validate_result(payload: Mapping[str, object]) -> SmokeResult:
    """校验四分支 exact 联合，不接受自报成功或混合 identity 形状。"""

    if set(payload) != _TOP_FIELDS or payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("failover live smoke schema is invalid")
    result = deepcopy(dict(payload))
    status = result["status"]
    provider_called = result["provider_called"]
    attempt_count = result["attempt_count"]
    chain_id = result["chain_id"]
    selected = result["selected_ordinal"]
    reason = result["reason_code"]
    if status not in {"passed", "hosted-unverified", "external-blocked", "failed"}:
        raise ValueError("failover live smoke status is invalid")
    if not isinstance(provider_called, bool) or not _non_bool_int(attempt_count):
        raise ValueError("failover live smoke invocation counts are invalid")
    if selected is not None and not _non_bool_int(selected, minimum=1):
        raise ValueError("failover live selected ordinal is invalid")

    empty_identity = chain_id is None and selected is None and result["candidates"] == []
    if status == "hosted-unverified":
        if (
            reason not in _HOSTED_REASONS
            or provider_called
            or attempt_count != 0
            or not empty_identity
            or result["usage"] is not None
        ):
            raise ValueError("hosted-unverified failover result is inconsistent")
        return result
    if status == "failed" and empty_identity:
        if (
            reason != "contract_failure"
            or provider_called
            or attempt_count != 0
            or result["usage"] is not None
        ):
            raise ValueError("pre-freeze failed result is inconsistent")
        return result

    if not isinstance(chain_id, str) or re.fullmatch(r"[0-9a-f]{64}", chain_id) is None:
        raise ValueError("frozen failover result requires a chain id")
    candidates = _validate_candidates(result["candidates"])
    if len(candidates) != 2 or [item["ordinal"] for item in candidates] != [1, 2]:
        raise ValueError("frozen failover result requires two candidates")
    if attempt_count != sum(cast(int, item["attempt_count"]) for item in candidates):
        raise ValueError("failover live top-level attempt count is inconsistent")
    any_observed = any(item["request_sent"] or item["response_observed"] for item in candidates)
    if provider_called != any_observed:
        raise ValueError("failover live provider-called high-water mark is inconsistent")
    usage = _validate_usage(result["usage"])
    completed = [
        cast(int, item["ordinal"]) for item in candidates if item["outcome"] == "completed"
    ]

    if status == "passed":
        first, second = candidates
        if (
            reason is not None
            or provider_called is not True
            or attempt_count != 2
            or selected != 2
            or first["outcome"] != "not_started"
            or first["attempt_count"] != 1
            or first["not_started_proof_count"] != 1
            or second["outcome"] != "completed"
            or second["attempt_count"] != 1
            or second["not_started_proof_count"] != 0
            or second["request_sent"] is not True
            or second["response_observed"] is not True
            or second["not_started_reason"] is not None
            or not isinstance(second["http_status"], int)
            or isinstance(second["http_status"], bool)
            or not 200 <= second["http_status"] <= 299
            or completed != [2]
            or usage is None
        ):
            raise ValueError("passed failover live result is inconsistent")
    elif status == "external-blocked":
        if reason not in _EXTERNAL_REASONS or selected is not None or completed:
            raise ValueError("external-blocked failover result is inconsistent")
    elif status == "failed":
        if reason != "contract_failure":
            raise ValueError("failed failover result reason is invalid")
        if selected is not None and completed != [selected]:
            raise ValueError("failed failover selected candidate is inconsistent")
    else:
        raise ValueError("hosted result cannot carry a frozen identity")
    return result


def validate_result_against_evidence(
    payload: Mapping[str, object],
    evidence: Mapping[str, object],
) -> SmokeResult:
    """把 artifact 逐值绑定到同一 durable chain、attempt、proof 与 usage。"""

    result = validate_result(payload)
    required = {"chain_id", "selected_ordinal", "candidates", "attempts", "usage"}
    if set(evidence) != required:
        raise ValueError("failover durable evidence shape is invalid")
    for field in ("chain_id", "selected_ordinal", "candidates", "usage"):
        if evidence[field] != result[field]:
            raise ValueError(f"failover artifact does not match durable {field}")
    attempts = evidence["attempts"]
    candidates = cast(list[dict[str, object]], result["candidates"])
    if (
        not isinstance(attempts, list)
        or len(cast(list[object], attempts)) != result["attempt_count"]
    ):
        raise ValueError("failover durable attempts are invalid")
    attempt_items = cast(list[object], attempts)
    expected_attempt = 1
    for candidate in candidates:
        ordinal = cast(int, candidate["ordinal"])
        count = cast(int, candidate["attempt_count"])
        proof_count = cast(int, candidate["not_started_proof_count"])
        for local_index in range(count):
            item = attempt_items[expected_attempt - 1]
            if not isinstance(item, Mapping) or set(cast(Mapping[object, object], item)) != {
                "attempt",
                "candidate_ordinal",
                "not_started_proof_count",
            }:
                raise ValueError("failover durable attempt binding is invalid")
            attempt = _typed_mapping(cast(Mapping[object, object], item))
            if (
                attempt["attempt"] != expected_attempt
                or attempt["candidate_ordinal"] != ordinal
                or attempt["not_started_proof_count"] != (1 if local_index < proof_count else 0)
            ):
                raise ValueError("failover durable attempt binding is invalid")
            expected_attempt += 1
    return result


__all__ = [
    "SCHEMA_VERSION",
    "SmokeResult",
    "preflight_result",
    "validate_preflight_routes",
    "validate_result",
    "validate_result_against_evidence",
]
