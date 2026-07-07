"""DBOS runtime adapter 边界。

core runtime 不直接 import DBOS；后续 service profile 要接耐久执行时，只能在这个
批准的 adapter 路径实现接口。
"""

from __future__ import annotations

from typing import Protocol


class DBOSRuntimeAdapter(Protocol):
    name: str

    async def enqueue_run(self, run_id: str) -> None:
        """把 run 交给 service profile 的耐久执行后端。"""


class NoopDBOSRuntimeAdapter:
    name = "dbos"

    async def enqueue_run(self, run_id: str) -> None:
        return None
