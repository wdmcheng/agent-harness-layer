"""DBOS runtime adapter boundary.

The core runtime deliberately does not import DBOS. A future service adapter
can implement this interface in this approved adapter path.
"""

from __future__ import annotations

from typing import Protocol


class DBOSRuntimeAdapter(Protocol):
    name: str

    async def enqueue_run(self, run_id: str) -> None:
        """Enqueue or start durable run processing in a service profile."""


class NoopDBOSRuntimeAdapter:
    name = "dbos"

    async def enqueue_run(self, run_id: str) -> None:
        return None
