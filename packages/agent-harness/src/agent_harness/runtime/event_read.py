"""RUN-003、RUN-006 与 CLI 共用的只读 run ownership 授权。"""

from __future__ import annotations

from dataclasses import dataclass

from agent_harness.identity import IdentityContext
from agent_harness.storage import SQLAlchemyStorage


@dataclass(frozen=True, slots=True)
class RunReadAuthorization:
    """已验证 tenant ownership 的最小稳定摘要，不暴露 ORM record。"""

    run_id: str
    tenant_id: str
    trace_id: str


async def authorize_run_read(
    storage: SQLAlchemyStorage,
    *,
    run_id: str,
    identity: IdentityContext,
) -> RunReadAuthorization:
    """只读校验 run ownership；不得补写 terminal、audit 或其他 evidence。"""

    async with storage.uow() as uow:
        run = await uow.runs.get(run_id)
    if run is None or run.tenant_id != identity.tenant_id:
        raise LookupError(f"run not found: {run_id}")
    return RunReadAuthorization(
        run_id=run_id,
        tenant_id=run.tenant_id,
        trace_id=run.trace_id,
    )


__all__ = ["RunReadAuthorization", "authorize_run_read"]
