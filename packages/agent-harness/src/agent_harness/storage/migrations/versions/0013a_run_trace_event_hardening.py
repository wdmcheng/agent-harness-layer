"""以前滚 revision 收敛已发布 0013 的 CanonicalEvent shape 漂移。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from agent_harness.storage.migrations.versions._run_trace_event_hardening_0013a.check_sql import (
    EXPECTED_CHECK_SIGNATURES,
    CheckExpressionParser,
    CheckParseError,
)
from agent_harness.storage.migrations.versions._run_trace_event_hardening_0013a.schema import (
    AUDIT_SCOPE as _AUDIT_SCOPE,
)
from agent_harness.storage.migrations.versions._run_trace_event_hardening_0013a.schema import (
    EVENT_NON_RUN_OWNERSHIP as _EVENT_NON_RUN_OWNERSHIP,
)
from agent_harness.storage.migrations.versions._run_trace_event_hardening_0013a.schema import (
    EVENT_RUN_OWNER as _EVENT_RUN_OWNER,
)
from agent_harness.storage.migrations.versions._run_trace_event_hardening_0013a.schema import (
    EVENT_RUN_OWNERSHIP as _EVENT_RUN_OWNERSHIP,
)
from agent_harness.storage.migrations.versions._run_trace_event_hardening_0013a.schema import (
    EVENT_SCOPE as _EVENT_SCOPE,
)
from agent_harness.storage.migrations.versions._run_trace_event_hardening_0013a.schema import (
    EVENT_SEQUENCE as _EVENT_SEQUENCE,
)
from agent_harness.storage.migrations.versions._run_trace_event_hardening_0013a.schema import (
    EVENT_STREAM_INDEX as _EVENT_STREAM_INDEX,
)
from agent_harness.storage.migrations.versions._run_trace_event_hardening_0013a.schema import (
    OLD_EVENT_SEQUENCE as _OLD_EVENT_SEQUENCE,
)
from agent_harness.storage.migrations.versions._run_trace_event_hardening_0013a.schema import (
    RUN_OWNER_KEY as _RUN_OWNER_KEY,
)
from agent_harness.storage.migrations.versions._run_trace_event_hardening_0013a.schema import (
    classify_shape as _classify_shape,
)
from agent_harness.storage.migrations.versions._run_trace_event_hardening_0013a.schema import (
    invalid_shape as _invalid_shape,
)
from agent_harness.storage.migrations.versions._run_trace_event_hardening_0013a.schema import (
    preflight_final_data as _preflight_final_data,
)
from agent_harness.storage.migrations.versions._run_trace_event_hardening_0013a.schema import (
    preflight_legacy_data as _preflight_legacy_data,
)
from agent_harness.storage.migrations.versions._run_trace_event_hardening_0013a.schema import (
    require_supported_dialect as _require_supported_dialect,
)

revision = "0013a_run_trace_event_hardening"
down_revision = "0013_run_trace_correlation"
branch_labels = None
depends_on = None

# 这些名称是已发布 revision 的测试诊断接缝；拆分后继续原样暴露。
_CheckExpressionParser = CheckExpressionParser
_CheckParseError = CheckParseError
_EXPECTED_CHECK_SIGNATURES = EXPECTED_CHECK_SIGNATURES


def upgrade() -> None:
    """验证旧/新 0013 的完整 shape，并只对旧 shape 原子前滚。"""

    connection = op.get_bind()
    _require_supported_dialect(connection)
    source = _classify_shape(connection)
    if source == "final":
        _preflight_final_data(connection)
        return

    _preflight_legacy_data(connection)
    with op.batch_alter_table("agent_runs") as batch_op:
        batch_op.create_unique_constraint(
            _RUN_OWNER_KEY,
            ["id", "tenant_id", "trace_id"],
        )

    with op.batch_alter_table("canonical_events") as batch_op:
        batch_op.drop_constraint(_OLD_EVENT_SEQUENCE, type_="unique")
        batch_op.alter_column(
            "run_id",
            existing_type=sa.String(length=36),
            nullable=True,
        )
        batch_op.alter_column(
            "trace_id",
            existing_type=sa.String(length=128),
            nullable=True,
        )
        batch_op.add_column(sa.Column("stream_id", sa.String(length=128), nullable=True))

    # 旧 0013 把公开 stream 暂存在 run_id。复制是已发布合同的确定性搬迁；
    # non-run 的数据库 ownership 随后清空，绝不据 envelope 猜 AgentRun。
    connection.execute(sa.text("update canonical_events set stream_id = run_id"))
    connection.execute(
        sa.text("update canonical_events set run_id = null where record_scope = 'non_run'")
    )

    with op.batch_alter_table("canonical_events") as batch_op:
        batch_op.alter_column(
            "stream_id",
            existing_type=sa.String(length=128),
            nullable=False,
        )
        batch_op.create_unique_constraint(
            _EVENT_SEQUENCE,
            ["tenant_id", "stream_id", "seq"],
        )
        batch_op.create_check_constraint(
            _EVENT_SCOPE,
            "record_scope IN ('run', 'non_run')",
        )
        batch_op.create_check_constraint(
            _EVENT_RUN_OWNERSHIP,
            "record_scope != 'run' OR (run_id IS NOT NULL AND trace_id IS NOT NULL)",
        )
        batch_op.create_check_constraint(
            _EVENT_NON_RUN_OWNERSHIP,
            "record_scope != 'non_run' OR run_id IS NULL",
        )
        batch_op.create_foreign_key(
            _EVENT_RUN_OWNER,
            "agent_runs",
            ["run_id", "tenant_id", "trace_id"],
            ["id", "tenant_id", "trace_id"],
            deferrable=True,
            initially="DEFERRED",
        )
    op.create_index(_EVENT_STREAM_INDEX, "canonical_events", ["stream_id"])
    with op.batch_alter_table("audit_logs") as batch_op:
        batch_op.create_check_constraint(
            _AUDIT_SCOPE,
            "record_scope IN ('run', 'non_run')",
        )


def downgrade() -> None:
    """只回退 revision stamp，保留已经统一的 0013 schema 与全部 evidence。

    0013 的历史已发布 shape 不唯一，无法安全判断哪些列和约束可以删除。真正的
    破坏性回退仍由 0013 自己的 evidence-aware opt-in 门禁负责；降到这个 revision
    后普通运行入口会因未达到当前 head 而 fail closed。
    """

    connection = op.get_bind()
    _require_supported_dialect(connection)
    if _classify_shape(connection) != "final":  # pragma: no cover - defensive invariant
        _invalid_shape()
    _preflight_final_data(connection)
