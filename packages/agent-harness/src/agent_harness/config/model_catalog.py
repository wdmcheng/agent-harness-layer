"""受信模型目录的 canonical Decimal 与 digest 实现。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

MAX_BUDGET_INTEGER = (1 << 63) - 1


def checked_budget_add(*values: int) -> int:
    """在 signed BIGINT 域内执行预算加法，拒绝 bool、负数与溢出。"""

    total = 0
    for value in values:
        if type(value) is not int or not 0 <= value <= MAX_BUDGET_INTEGER:
            raise ValueError("budget integer is outside signed BIGINT")
        if total > MAX_BUDGET_INTEGER - value:
            raise ValueError("budget integer addition overflow")
        total += value
    return total


def checked_budget_mul(first: int, second: int) -> int:
    """在 signed BIGINT 域内执行预算乘法，避免持久化前由 Python 无限整数掩盖溢出。"""

    if (
        type(first) is not int
        or type(second) is not int
        or not 0 <= first <= MAX_BUDGET_INTEGER
        or not 0 <= second <= MAX_BUDGET_INTEGER
        or (second and first > MAX_BUDGET_INTEGER // second)
    ):
        raise ValueError("budget integer multiplication overflow")
    return first * second


def canonical_decimal(value: object) -> str | None:
    """生成无 exponent、无无意义尾零且不含负零的价格字符串。"""

    if value is None:
        return None
    decimal = value if isinstance(value, Decimal) else Decimal(str(value))
    if not decimal.is_finite() or decimal < 0:
        raise ValueError("catalog decimal must be finite and non-negative")
    if decimal == 0:
        return "0"
    rendered = format(decimal, "f").rstrip("0").rstrip(".")
    return rendered or "0"


def model_catalog_digest(ref: str, entry: Mapping[str, object]) -> str:
    """按request shape判别的`model-catalog/v1|v2`冻结形状计算SHA-256。"""

    tool_enabled = entry.get("request_shape_ref") == "single-user-text-with-tool-catalog"
    payload: dict[str, Any] = {
        "schema_version": "model-catalog/v2" if tool_enabled else "model-catalog/v1",
        "model_catalog_ref": ref,
        "model_catalog_version": entry.get("version"),
        "provider_kind": entry.get("provider_kind"),
        "model": entry.get("model"),
        "request_shape_ref": entry.get("request_shape_ref"),
        "request_shape_version": entry.get("request_shape_version"),
        "input_bound_strategy_ref": entry.get("input_bound_strategy_ref"),
        "input_bound_strategy_version": entry.get("input_bound_strategy_version"),
        "input_envelope_token_bound": entry.get("input_envelope_token_bound"),
        "cost_enabled": entry.get("cost_enabled"),
        "input_token_price_usd": canonical_decimal(entry.get("input_token_price_usd")),
        "output_token_price_usd": canonical_decimal(entry.get("output_token_price_usd")),
        "price_source_ref": entry.get("price_source_ref"),
        "price_source_version": entry.get("price_source_version"),
    }
    if tool_enabled:
        payload["max_tool_catalog_utf8_bytes"] = entry.get("max_tool_catalog_utf8_bytes")
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "MAX_BUDGET_INTEGER",
    "canonical_decimal",
    "checked_budget_add",
    "checked_budget_mul",
    "model_catalog_digest",
]
