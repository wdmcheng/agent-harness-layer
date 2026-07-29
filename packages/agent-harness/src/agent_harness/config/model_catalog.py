"""受信模型目录的 canonical Decimal 与 digest 实现。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from decimal import Decimal
from typing import Any


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
    """按 `model-catalog/v1` 的冻结 JSON 形状计算 SHA-256。"""

    payload: dict[str, Any] = {
        "schema_version": "model-catalog/v1",
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
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = ["canonical_decimal", "model_catalog_digest"]
