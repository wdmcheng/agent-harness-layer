"""增加 usage settlement、ordered outbox 与 run event capacity。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import context, op

revision = "0014_run_evidence_outbox"
down_revision = "0013a_run_trace_event_hardening"
branch_labels = None
depends_on = None

_OPT_IN = "allow_empty_evidence_downgrade=true"
_MAX_EVENT_SEQ = 2_147_483_647
_OPERATION_REGISTRY_VERSION = "1"
_OPERATION_CAPACITY = {
    "approval_resolution": 1,
    "tool_invocation": 3,
}
_RUN_STATUSES = frozenset({"created", "running", "waiting", "completed", "failed", "cancelled"})
_TERMINAL_RUN_STATUSES = frozenset({"completed", "failed", "cancelled"})
_ACTIVE_APPROVAL_STATES = frozenset(
    {
        "claimed",
        "execution_owned",
        "recovery_pending",
        "completed",
        "failed",
        "denied_pending",
        "needs_review",
    }
)
_FINAL_APPROVAL_STATES = frozenset({"completed", "failed", "denied"})
_TOOL_EXECUTION_STATES = frozenset({"executing", "completed", "failed"})


class _CapacityBackfill:
    """Alembic 动态模块加载下不依赖 dataclass 的不可变回填记录。"""

    __slots__ = ("run_id", "tenant_id", "highest_seq", "outstanding", "terminal_reservation")

    def __init__(
        self,
        *,
        run_id: str,
        tenant_id: str,
        highest_seq: int,
        outstanding: int,
        terminal_reservation: int,
    ) -> None:
        self.run_id = run_id
        self.tenant_id = tenant_id
        self.highest_seq = highest_seq
        self.outstanding = outstanding
        self.terminal_reservation = terminal_reservation


def upgrade() -> None:
    connection = op.get_bind()
    backfill = _preflight_capacity_backfill(connection)
    op.create_table(
        "run_event_capacity",
        sa.Column("run_id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("highest_persisted_seq", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "outstanding_reserved_event_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("terminal_reservation", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(
            ["run_id", "tenant_id"],
            ["agent_runs.id", "agent_runs.tenant_id"],
            name="fk_run_event_capacity_run_tenant",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("tenant_id", "run_id", name="uq_run_event_capacity_tenant_run"),
        sa.CheckConstraint(
            "highest_persisted_seq >= 0 AND "
            "outstanding_reserved_event_count >= 0 AND "
            "terminal_reservation IN (0, 1)",
            name="ck_run_event_capacity_non_negative",
        ),
        sa.CheckConstraint(
            "highest_persisted_seq + outstanding_reserved_event_count "
            "+ terminal_reservation <= 2147483647",
            name="ck_run_event_capacity_total",
        ),
    )
    op.create_table(
        "run_evidence_outbox",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("usage_call_id", sa.String(length=128), nullable=True),
        sa.Column("event_id", sa.String(length=128), nullable=False),
        sa.Column("operation_kind", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=True),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("reserved_event_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("group_id", sa.String(length=128), nullable=True),
        sa.Column("sequence_in_group", sa.Integer(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(
            ["run_id", "tenant_id"],
            ["agent_runs.id", "agent_runs.tenant_id"],
            name="fk_run_evidence_outbox_run_tenant",
        ),
        sa.UniqueConstraint(
            "tenant_id", "usage_call_id", name="uq_run_evidence_outbox_tenant_usage_call"
        ),
        sa.UniqueConstraint("event_id", name="uq_run_evidence_outbox_event_id"),
        sa.UniqueConstraint(
            "group_id",
            "sequence_in_group",
            name="uq_run_evidence_outbox_group_sequence",
        ),
        sa.CheckConstraint(
            "(group_id IS NULL AND sequence_in_group IS NULL) OR "
            "(group_id IS NOT NULL AND sequence_in_group > 0)",
            name="ck_run_evidence_outbox_group_shape",
        ),
    )
    op.create_index(
        "ix_run_evidence_outbox_usage_call_id", "run_evidence_outbox", ["usage_call_id"]
    )
    op.create_index("ix_run_evidence_outbox_event_id", "run_evidence_outbox", ["event_id"])
    op.create_index("ix_run_evidence_outbox_state", "run_evidence_outbox", ["state"])
    op.create_index("ix_run_evidence_outbox_group_id", "run_evidence_outbox", ["group_id"])

    for row in backfill:
        connection.execute(
            sa.text(
                "insert into run_event_capacity "
                "(run_id, tenant_id, highest_persisted_seq, "
                "outstanding_reserved_event_count, terminal_reservation) "
                "values (:run_id, :tenant_id, :highest_seq, "
                ":outstanding_reserved_event_count, :terminal_reservation)"
            ),
            {
                "run_id": row.run_id,
                "tenant_id": row.tenant_id,
                "highest_seq": row.highest_seq,
                "outstanding_reserved_event_count": row.outstanding,
                "terminal_reservation": row.terminal_reservation,
            },
        )


def downgrade() -> None:
    _require_exact_empty_evidence_opt_in()
    connection = op.get_bind()
    evidence_tables = ("run_evidence_outbox", "run_event_capacity")
    for table in evidence_tables:
        if _table_count(connection, table):
            raise RuntimeError("0014 downgrade refused: evidence exists")
    op.drop_index("ix_run_evidence_outbox_state", table_name="run_evidence_outbox")
    op.drop_index("ix_run_evidence_outbox_group_id", table_name="run_evidence_outbox")
    op.drop_index("ix_run_evidence_outbox_event_id", table_name="run_evidence_outbox")
    op.drop_index("ix_run_evidence_outbox_usage_call_id", table_name="run_evidence_outbox")
    op.drop_table("run_evidence_outbox")
    op.drop_table("run_event_capacity")


def _preflight_capacity_backfill(connection: sa.Connection) -> list[_CapacityBackfill]:
    invalid = connection.execute(
        sa.text(
            "select count(*) from canonical_events ce "
            "left join agent_runs ar on ar.id = ce.run_id and ar.tenant_id = ce.tenant_id "
            "where ce.record_scope = 'run' and ar.id is null"
        )
    ).scalar_one()
    if invalid:
        raise RuntimeError("0014 upgrade refused: canonical event stream owner is unknown")

    # 0013a 会保留 non-run telemetry 的 synthetic stream_id；它不拥有 AgentRun，
    # 但仍占用同一物理 stream 的 seq 唯一空间，所以 high-water 必须按 stream_id
    # 计算，不能只看 run-scoped ownership 列 run_id。
    run_rows = connection.execute(
        sa.text(
            """
            select ar.id as run_id, ar.tenant_id, ar.status,
                   coalesce(max(ce.seq), 0) as highest_seq,
                   coalesce(sum(case when ce.terminal then 1 else 0 end), 0) as terminal_count,
                   coalesce(max(case when ce.terminal then ce.seq else 0 end), 0) as terminal_seq
            from agent_runs ar
            left join canonical_events ce
              on ce.tenant_id = ar.tenant_id and ce.stream_id = ar.id
            group by ar.id, ar.tenant_id, ar.status
            order by ar.id
            """
        )
    ).mappings()
    active_approval_counts = _active_approval_counts(connection)
    active_tool_counts = _active_tool_counts(connection)
    backfill: list[_CapacityBackfill] = []
    for row in run_rows:
        run_id = str(row["run_id"])
        tenant_id = str(row["tenant_id"])
        status = str(row["status"])
        highest_seq = int(row["highest_seq"])
        terminal_count = int(row["terminal_count"])
        terminal_seq = int(row["terminal_seq"])
        if status not in _RUN_STATUSES:
            raise RuntimeError("0014 upgrade refused: run status is unknown")
        if highest_seq < 0 or highest_seq > _MAX_EVENT_SEQ:
            raise RuntimeError("0014 upgrade refused: canonical event sequence is invalid")
        if terminal_count not in {0, 1} or (terminal_count == 1 and terminal_seq != highest_seq):
            raise RuntimeError("0014 upgrade refused: run terminal evidence is contradictory")
        if (status in _TERMINAL_RUN_STATUSES) != (terminal_count == 1):
            raise RuntimeError("0014 upgrade refused: run status and terminal evidence disagree")
        outstanding = active_approval_counts.get(run_id, 0) + active_tool_counts.get(run_id, 0)
        terminal_reservation = 0 if terminal_count else 1
        if terminal_count and outstanding:
            raise RuntimeError("0014 upgrade refused: terminal run has active operation")
        if highest_seq + outstanding + terminal_reservation > _MAX_EVENT_SEQ:
            raise RuntimeError("0014 upgrade refused: event capacity is exhausted")
        backfill.append(
            _CapacityBackfill(
                run_id=run_id,
                tenant_id=tenant_id,
                highest_seq=highest_seq,
                outstanding=outstanding,
                terminal_reservation=terminal_reservation,
            )
        )
    return backfill


def _active_approval_counts(connection: sa.Connection) -> dict[str, int]:
    counts: dict[str, int] = {}
    rows = connection.execute(
        sa.text(
            "select run_id, status, resolution_state from approvals "
            "where resolution_state is not null"
        )
    ).mappings()
    for row in rows:
        run_id = str(row["run_id"])
        status = str(row["status"])
        state = str(row["resolution_state"])
        if status == "waiting":
            if state not in _ACTIVE_APPROVAL_STATES:
                raise RuntimeError("0014 upgrade refused: approval operation state is unknown")
            counts[run_id] = counts.get(run_id, 0) + _OPERATION_CAPACITY["approval_resolution"]
        elif status in {"approved", "denied"}:
            if state not in _FINAL_APPROVAL_STATES:
                raise RuntimeError(
                    "0014 upgrade refused: finalized approval state is contradictory"
                )
        else:
            raise RuntimeError("0014 upgrade refused: approval status is unknown")
    return counts


def _active_tool_counts(connection: sa.Connection) -> dict[str, int]:
    counts: dict[str, int] = {}
    rows = connection.execute(
        sa.text(
            "select run_id, execution_state from tool_invocations "
            "where run_id is not null and execution_state is not null"
        )
    ).mappings()
    for row in rows:
        run_id = str(row["run_id"])
        state = str(row["execution_state"])
        if state not in _TOOL_EXECUTION_STATES:
            raise RuntimeError("0014 upgrade refused: tool operation state is unknown")
        if state == "executing":
            counts[run_id] = counts.get(run_id, 0) + _OPERATION_CAPACITY["tool_invocation"]
    return counts


def _table_count(connection: sa.Connection, table: str) -> int:
    return int(connection.execute(sa.text(f"select count(*) from {table}")).scalar_one())


def _require_exact_empty_evidence_opt_in() -> None:
    arguments = context.get_x_argument(as_dictionary=False)
    if arguments != [_OPT_IN]:
        raise RuntimeError("0014 downgrade refused: explicit opt-in is required")
