"""0013 run trace 迁移合同共享的 SQLite fixture 与关系清单。"""

from __future__ import annotations

import json
import sqlite3
from argparse import Namespace
from pathlib import Path

from alembic.config import Config

from agent_harness.storage import run_migrations
from agent_harness.storage.migrations.runner import alembic_config

RUN_RELATION_TABLES = (
    "approvals",
    "artifacts",
    "canonical_events",
    "checkpoints",
    "context_assemblies",
    "eval_cases",
    "eval_runs",
    "eval_scores",
    "tool_invocations",
    "trace_refs",
    "workspaces",
)

INVALID_RUN_RELATION_IDS = {
    "approvals": "invalid-approval",
    "artifacts": "invalid-artifact",
    "canonical_events": "invalid-canonical-event",
    "checkpoints": "invalid-checkpoint",
    "context_assemblies": "invalid-context-assembly",
    "eval_cases": "invalid-eval-case",
    "eval_runs": "invalid-eval-run",
    "eval_scores": "invalid-eval-score",
    "tool_invocations": "invalid-tool-invocation",
    "trace_refs": "invalid-trace-ref",
    "workspaces": "invalid-workspace",
}


def sqlite_dsn(path: Path) -> str:
    """为隔离迁移数据库生成异步 SQLite DSN，避免共享测试文件导致 revision 串扰。"""

    return f"sqlite+aiosqlite:///{path}"


def migration_config(dsn: str, *, x_args: list[str] | None = None) -> Config:
    """构造携带可控 Alembic ``-x`` 参数的迁移配置，供升级/降级保护测试复用。"""

    config = alembic_config(dsn)
    config.cmd_opts = Namespace(x=x_args or [])
    return config


def seed_identity(connection: sqlite3.Connection, tenant_id: str) -> None:
    """写入满足 run 外键前提的最小 tenant 与 session，不掺入与 trace 无关的业务数据。"""

    connection.execute(
        "insert into tenants(id, display_name) values (?, ?)",
        (tenant_id, tenant_id),
    )
    connection.execute(
        "insert into sessions(id, tenant_id, user_id, metadata_json) values (?, ?, ?, '{}')",
        (f"session-{tenant_id}", tenant_id, f"user-{tenant_id}"),
    )


def seed_run(
    connection: sqlite3.Connection,
    run_id: str,
    *,
    tenant_id: str = "tenant-a",
    parent_run_id: str | None = None,
    trace_id: object = None,
) -> None:
    """插入可选父关系和 trace 上下文的旧版 run 行，为迁移前后约束映射提供受控基线。"""

    context = {} if trace_id is None else {"trace_id": trace_id}
    connection.execute(
        """
        insert into agent_runs(
            id, tenant_id, session_id, agent_id, status, parent_run_id,
            input_json, execution_context_json
        ) values (?, ?, ?, 'agent-a', 'created', ?, '{}', ?)
        """,
        (run_id, tenant_id, f"session-{tenant_id}", parent_run_id, json.dumps(context)),
    )


def prepare_0012a(path: Path) -> None:
    """将临时库升级到 run-trace 迁移之前的既定 revision，避免测试从最新 schema 失去意义。"""

    run_migrations(sqlite_dsn(path), "0012a_embedding_cache_tenant_scope")


def seed_invalid_run_relation(
    connection: sqlite3.Connection,
    table: str,
    *,
    tenant_id: str,
    run_id: str,
) -> None:
    """向指定关联表植入跨 run 的非法旧数据，检验迁移预检能逐表拒绝脏关系。"""

    statements = {
        "approvals": (
            "insert into approvals("
            "id, tenant_id, run_id, agent_id, action, resource, reason, status, "
            "metadata_json, trace_id"
            ") values ('invalid-approval', ?, ?, 'agent-a', 'write', 'file:a', "
            "'review', 'waiting', '{}', null)"
        ),
        "artifacts": (
            "insert into artifacts("
            "id, tenant_id, run_id, artifact_type, uri, checksum_sha256, size_bytes, "
            "metadata_json"
            ") values ('invalid-artifact', ?, ?, 'result', 'artifact://invalid', "
            "'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 1, '{}')"
        ),
        "canonical_events": (
            "insert into canonical_events("
            "id, tenant_id, run_id, event_type, seq, terminal, visibility, trace_id, "
            "envelope_json"
            ") values ('invalid-canonical-event', ?, ?, 'run.started', 1, 0, "
            "'public', null, '{}')"
        ),
        "checkpoints": (
            "insert into checkpoints("
            "id, tenant_id, run_id, sequence, resume_token, state_json"
            ") values ('invalid-checkpoint', ?, ?, 1, 'invalid-resume', '{}')"
        ),
        "context_assemblies": (
            "insert into context_assemblies("
            "id, tenant_id, run_id, input_refs_json, token_budget, trust_summary_json, "
            "truncation_summary_json, output_ref"
            ") values ('invalid-context-assembly', ?, ?, '[]', 1, '{}', '{}', "
            "'artifact://context')"
        ),
        "eval_cases": (
            "insert into eval_cases("
            "id, tenant_id, name, status, payload_json, run_id, trace_id"
            ") values ('invalid-eval-case', ?, 'invalid', 'draft', '{}', ?, null)"
        ),
        "trace_refs": (
            "insert into trace_refs("
            "id, tenant_id, run_id, provider, external_trace_id"
            ") values ('invalid-trace-ref', ?, ?, 'provider-a', 'external-a')"
        ),
        "eval_runs": (
            "insert into eval_runs(id, tenant_id, run_id, status) "
            "values ('invalid-eval-run', ?, ?, 'completed')"
        ),
        "eval_scores": (
            "insert into eval_scores("
            "id, tenant_id, eval_run_id, case_id, run_id, trace_id, metric, value, "
            "metadata_json, provider_status_json"
            ") values ('invalid-eval-score', ?, 'support-eval-run', 'support-eval-case', "
            "?, null, 'quality', 1.0, '{}', '[]')"
        ),
        "tool_invocations": (
            "insert into tool_invocations("
            "id, tenant_id, agent_id, run_id, tool_name, args_ref, status, trace_id, "
            "metadata_json"
            ") values ('invalid-tool-invocation', ?, 'agent-a', ?, 'write', "
            "'artifact://args', 'completed', null, '{}')"
        ),
        "workspaces": (
            "insert into workspaces("
            "id, tenant_id, agent_id, run_id, root_path, policy_ref, metadata_json"
            ") values ('invalid-workspace', ?, 'agent-a', ?, '/tmp/workspace', "
            "'policy://default', '{}')"
        ),
    }
    if table == "eval_scores":
        connection.execute(
            "insert into eval_cases(id, tenant_id, name, status, payload_json) "
            "values ('support-eval-case', ?, 'support', 'approved', '{}')",
            (tenant_id,),
        )
        connection.execute(
            "insert into eval_runs(id, tenant_id, eval_case_id, status) "
            "values ('support-eval-run', ?, 'support-eval-case', 'completed')",
            (tenant_id,),
        )
    connection.execute(statements[table], (tenant_id, run_id))
