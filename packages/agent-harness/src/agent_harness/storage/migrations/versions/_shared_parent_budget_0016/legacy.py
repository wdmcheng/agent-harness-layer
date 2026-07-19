"""0016 legacy tree 终态与 durable recovery closure 校验。"""

# Alembic 的动态 table clause 在逐字段运行时校验后才使用；SQLAlchemy stubs
# 无法把这些 mapping 收窄成静态泛型，禁止 unknown 报告即可。
# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false

from __future__ import annotations

from collections.abc import Mapping, Sequence

import sqlalchemy as sa

from agent_harness.storage.migrations.versions._shared_parent_budget_0016.delegation import (
    _released_delegation_proof_valid,
)


def _require_legacy_closed(
    connection: sa.Connection,
    *,
    root: Mapping[str, object],
    children: Sequence[Mapping[str, object]],
) -> None:
    """逐树证明 legacy_closed；调用方可在失败后尝试完整 snapshot backfill。"""

    tree_ids = [str(root["id"]), *(str(value["id"]) for value in children)]
    if root["status"] not in {"completed", "failed", "cancelled"} or any(
        child["status"] not in {"completed", "failed", "cancelled"} for child in children
    ):
        raise RuntimeError("0016 legacy tree is active without immutable budget snapshot")
    terminal_types = {
        "completed": "run.completed",
        "failed": "run.failed",
        "cancelled": "run.cancelled",
    }
    if connection.dialect.name == "postgresql":
        for run_id, status in [
            (root["id"], root["status"]),
            *((child["id"], child["status"]) for child in children),
        ]:
            terminal_rows = connection.execute(
                sa.text(
                    "select event_type from canonical_events "
                    "where tenant_id=:tenant_id and run_id=:run_id and terminal=true"
                ),
                {"tenant_id": root["tenant_id"], "run_id": run_id},
            ).scalars()
            if list(terminal_rows) != [terminal_types[str(status)]]:
                raise RuntimeError("0016 legacy tree lacks PostgreSQL terminal closure proof")
    for run_id in tree_ids:
        pending_queue = connection.execute(
            sa.text(
                "select count(*) from agent_runs where tenant_id=:tenant_id and id=:run_id "
                "and (queue_operation_id is not null "
                "or queue_request_id is not null "
                "or queue_effective_idempotency_key is not null "
                "or queue_enqueue_state is not null "
                "or queue_message_id is not null "
                "or execution_owner_id is not null "
                "or execution_workflow_id is not null)"
            ),
            {"tenant_id": root["tenant_id"], "run_id": run_id},
        ).scalar_one()
        if pending_queue:
            raise RuntimeError("0016 legacy tree has pending queue recovery")
        capacity = (
            connection.execute(
                sa.text(
                    "select outstanding_reserved_event_count, terminal_reservation "
                    "from run_event_capacity where tenant_id=:tenant_id and run_id=:run_id"
                ),
                {"tenant_id": root["tenant_id"], "run_id": run_id},
            )
            .mappings()
            .one_or_none()
        )
        if (
            capacity is None
            or capacity["outstanding_reserved_event_count"] != 0
            or capacity["terminal_reservation"] != 0
        ):
            raise RuntimeError("0016 legacy tree lacks terminal capacity closure proof")
        pending_outbox = connection.execute(
            sa.text(
                "select count(*) from run_evidence_outbox "
                "where tenant_id=:tenant_id and run_id=:run_id "
                "and state not in ('published','cancelled')"
            ),
            {"tenant_id": root["tenant_id"], "run_id": run_id},
        ).scalar_one()
        if pending_outbox:
            raise RuntimeError("0016 legacy tree has pending evidence outbox")
        pending_approval = connection.execute(
            sa.text(
                "select count(*) from approvals where tenant_id=:tenant_id and run_id=:run_id "
                "and (status not in ('approved','denied') "
                "or resolution_state is null "
                "or resolution_state not in ('completed','failed') "
                "or resolution_operation_id is not null "
                "or resolution_enqueue_state is not null "
                "or resolution_message_id is not null "
                "or resolution_workflow_owner_id is not null "
                "or resolution_workflow_id is not null)"
            ),
            {"tenant_id": root["tenant_id"], "run_id": run_id},
        ).scalar_one()
        if pending_approval:
            raise RuntimeError("0016 legacy tree has pending approval recovery")
        pending_tool = connection.execute(
            sa.text(
                "select count(*) from tool_invocations "
                "where tenant_id=:tenant_id and run_id=:run_id and execution_state='executing'"
            ),
            {"tenant_id": root["tenant_id"], "run_id": run_id},
        ).scalar_one()
        if pending_tool:
            raise RuntimeError("0016 legacy tree has pending tool recovery")
    pending_delegation = connection.execute(
        sa.text(
            "select count(*) from agent_delegations d "
            "left join delegation_budget_reservations r "
            "on r.delegation_id=d.id and r.tenant_id=d.tenant_id "
            "where d.tenant_id=:tenant_id and d.parent_run_id=:root_id "
            "and (r.id is null "
            "or d.status not in ('completed','failed','released') "
            "or r.state not in ('settled','released'))"
        ),
        {"tenant_id": root["tenant_id"], "root_id": root["id"]},
    ).scalar_one()
    if pending_delegation:
        raise RuntimeError("0016 legacy tree has pending delegation evidence")

    released_delegation_ids = connection.execute(
        sa.text(
            "select d.id from agent_delegations d "
            "join delegation_budget_reservations r on r.delegation_id=d.id "
            "where d.tenant_id=:tenant_id and d.parent_run_id=:root_id "
            "and r.state='released'"
        ),
        {"tenant_id": root["tenant_id"], "root_id": root["id"]},
    ).scalars()
    for delegation_id in released_delegation_ids:
        if not _released_delegation_proof_valid(
            connection,
            tenant_id=root["tenant_id"],
            root_id=root["id"],
            delegation_id=str(delegation_id),
        ):
            raise RuntimeError("0016 legacy tree released delegation proof is invalid")


__all__ = ["_require_legacy_closed"]
