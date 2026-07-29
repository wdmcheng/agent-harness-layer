"""Shared-budget 私有稳定编码与 fail-closed 数值解析。"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal


def digest(value: object) -> str:
    """以稳定 JSON 编码生成快照/配置摘要，禁止 NaN 造成跨进程哈希漂移。"""

    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def price_from_snapshot(value: object) -> Decimal | None:
    """把快照 JSON 中的可选价格还原为有限非负 Decimal，损坏值一律 fail-closed。"""

    if value is None:
        return None
    try:
        price = Decimal(str(value))
    except Exception as exc:  # noqa: BLE001 - snapshot JSON 边界必须 fail closed
        raise ValueError("shared budget route price is invalid") from exc
    if not price.is_finite() or price < 0:
        raise ValueError("shared budget route price is invalid")
    return price


__all__ = ["digest", "price_from_snapshot"]
