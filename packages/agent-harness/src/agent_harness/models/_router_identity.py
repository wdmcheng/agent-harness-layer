"""Route-chain 所有 SHA-256 preimage 共用的唯一 canonical JSON 实现。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Literal, Protocol, cast


class RoutePlanIdentityView(Protocol):
    """canonical route identity 只读取的脱敏序列化能力。"""

    def model_dump(
        self,
        *,
        mode: Literal["python"],
        exclude: set[str],
    ) -> dict[str, object]: ...


def canonical_decimal(value: Decimal) -> str:
    """把有限非负 Decimal 变为无指数、无尾零、无负零的稳定字符串。"""

    if not value.is_finite() or value < 0:
        raise ValueError("route decimal must be finite and non-negative")
    normalized = format(value, "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return "0" if normalized in {"", "-0"} else normalized


def _reject_unsupported_json(value: object) -> None:
    """拒绝 serializer 契约之外的 float/Decimal 与非 JSON 容器。"""

    if isinstance(value, (float, Decimal)):
        raise TypeError("route canonical JSON requires pre-canonicalized decimals")
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        if any(not isinstance(key, str) for key in mapping):
            raise TypeError("route canonical JSON keys must be strings")
        for item in mapping.values():
            _reject_unsupported_json(item)
        return
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        for item in cast(Sequence[object], value):
            _reject_unsupported_json(item)
        return
    raise TypeError("route canonical JSON contains an unsupported value")


def model_route_canonical_json(value: Mapping[str, object]) -> bytes:
    """按 `model-route-canonical-json-v1` 生成 UTF-8 exact bytes。"""

    _reject_unsupported_json(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def model_route_digest(value: Mapping[str, object]) -> str:
    """对唯一 canonical bytes 计算小写 SHA-256。"""

    return hashlib.sha256(model_route_canonical_json(value)).hexdigest()


def route_plan_identity_payload(plan: RoutePlanIdentityView) -> dict[str, object]:
    """提取单候选完整但脱敏的冻结路由 identity。"""

    payload = plan.model_dump(mode="python", exclude={"canonical_base_url"})

    def normalize(item: object) -> object:
        if isinstance(item, Decimal):
            return canonical_decimal(item)
        if isinstance(item, Mapping):
            mapping = cast(Mapping[object, object], item)
            return {str(key): normalize(value) for key, value in mapping.items()}
        if isinstance(item, (tuple, list)):
            return [normalize(value) for value in cast(Sequence[object], item)]
        return item

    normalized = normalize(payload)
    return cast(dict[str, object], normalized)
