"""0016 preflight 共用的 canonical hash 与非负有限数值校验。"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation


def _canonical_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _decimal(value: object, *, field: str) -> Decimal:
    if isinstance(value, bool):
        raise RuntimeError(f"0016 backfill {field} is invalid")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise RuntimeError(f"0016 backfill {field} is invalid") from exc
    if not result.is_finite() or result < 0:
        raise RuntimeError(f"0016 backfill {field} is invalid")
    return result


def _integer(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeError(f"0016 backfill {field} is invalid")
    return value


__all__ = ["_canonical_hash", "_decimal", "_integer"]
