"""Storage、migration 和 UoW 的公开契约测试。

这些测试是后续 runtime/event/service profile 的地基，不是单纯检查私有函数：
每个用例都穿过调用方实际会使用的 public seam，例如 migration API、
Repository/UnitOfWork、CLI doctor 和静态边界扫描。注释刻意说明“锁什么”和
“不证明什么”，避免以后维护者把 SQLite 绿灯误读成 PostgreSQL 证据。
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from scripts.import_boundary_check import check_sqlalchemy_session_boundaries

from agent_harness.storage import SQLAlchemyStorage, ToolInvocationCreate, run_migrations
from agent_harness.storage.repositories import CheckpointCreate, RunCreate, SessionCreate

ROOT = Path(__file__).resolve().parents[2]
PROFILES = ROOT / "templates" / "service-app" / "configs" / "profiles"


def sqlite_dsn(path: Path) -> str:
    """生成 storage 合同测试专用 SQLite DSN。"""

    # 测试使用独立临时库，避免 migration 或 rollback 验证写入开发者本地 profile。
    return f"sqlite+aiosqlite:///{path}"


def assert_core_schema(db_path: Path) -> None:
    """断言迁移后核心表存在，避免只检查 revision 字符串。"""

    # 这里直接检查 SQLite catalog，因为 public seam 是“migration 后 schema 存在”。
    # Repository 行为另有 UoW 测试覆盖，避免一个测试同时承担两层证据。
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute("select name from sqlite_master where type='table'").fetchall()
        tables = {row[0] for row in rows}
        assert {
            "alembic_version",
            "tenants",
            "sessions",
            "agent_runs",
            "checkpoints",
            "canonical_events",
            "trace_refs",
            "artifacts",
            "eval_cases",
            "eval_runs",
            "policy_rules",
            "audit_logs",
            "context_assemblies",
            "tenant_embedding_cache",
            "run_trace_bindings",
            "retrieval_documents",
            "retrieval_chunks",
            "api_keys",
            "approvals",
            "workspaces",
            "tool_invocations",
            "eval_dataset_splits",
            "eval_experiments",
            "harness_acceptance_records",
        } <= tables
        revision = connection.execute("select version_num from alembic_version").fetchone()
        assert revision == ("0016_shared_parent_budget_ledger",)


def test_local_sqlite_migration_creates_core_schema(tmp_path: Path) -> None:
    """本地 SQLite 迁移必须初始化核心表；该测试只证明本地 schema，不替代服务数据库证据。"""

    # local migration 是 storage contract 的最低门槛：没有外部服务也必须能初始化核心表。
    # 它只证明 SQLite schema，不被拿来替代 PostgreSQL service migration 证据。
    db_path = tmp_path / "agent_harness.db"

    run_migrations(sqlite_dsn(db_path))

    assert_core_schema(db_path)


@pytest.mark.asyncio
async def test_repository_contract_uses_uow_and_rolls_back(tmp_path: Path) -> None:
    """公开仓储只能经 UoW 事务提交；未显式提交时离开上下文必须回滚，防止运行错误留下脏数据。"""

    # 这个用例锁 repository + UoW 的公开事务语义：调用方只看 DTO 和 commit/rollback，
    # 不接触 SQLAlchemy AsyncSession。rollback 分支防止未来把 context manager 改成
    # “离开就自动提交”，那会让 runtime/checkpoint 的错误路径产生脏数据。
    db_path = tmp_path / "agent_harness.db"
    dsn = sqlite_dsn(db_path)
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)

    try:
        async with storage.uow() as uow:
            tenant = await uow.tenants.ensure("default")
            session = await uow.sessions.create(
                SessionCreate(
                    tenant_id=tenant.id,
                    user_id="local-user",
                    agent_id="fake-agent",
                )
            )
            run = await uow.runs.create(
                RunCreate(
                    tenant_id=tenant.id,
                    session_id=session.id,
                    agent_id="fake-agent",
                    idempotency_key="idem-1",
                    trace_id="trace-idem-1",
                    input={"prompt": "hello"},
                )
            )
            checkpoint = await uow.checkpoints.create(
                CheckpointCreate(
                    tenant_id=tenant.id,
                    run_id=run.id,
                    sequence=1,
                    resume_token="resume-1",
                    state={"step": "created", "trace_id": "trace-idem-1"},
                )
            )
            await uow.commit()

        async with storage.uow() as uow:
            same_run = await uow.runs.get_by_idempotency_key(
                tenant_id="default",
                session_id=session.id,
                agent_id="fake-agent",
                idempotency_key="idem-1",
            )
            latest = await uow.checkpoints.get_latest(run.id)

        assert same_run is not None
        assert same_run.id == run.id
        assert latest is not None
        assert latest.id == checkpoint.id
        assert latest.state == {"step": "created", "trace_id": "trace-idem-1"}

        async with storage.uow() as uow:
            rolled_back = await uow.runs.create(
                RunCreate(
                    tenant_id="default",
                    session_id=session.id,
                    agent_id="fake-agent",
                    idempotency_key="rollback",
                    trace_id="trace-rollback",
                    input={},
                )
            )
            assert rolled_back.id

        async with storage.uow() as uow:
            missing = await uow.runs.get_by_idempotency_key(
                tenant_id="default",
                session_id=session.id,
                agent_id="fake-agent",
                idempotency_key="rollback",
            )
        assert missing is None
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_0008_keeps_previous_tool_repository_writes_compatible(tmp_path: Path) -> None:
    """新增 nullable claim 列不能破坏上一版本不传这些字段的基础 UoW 写入。"""

    db_path = tmp_path / "forward-compatible.db"
    dsn = sqlite_dsn(db_path)
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    try:
        async with storage.uow() as uow:
            tenant = await uow.tenants.ensure("forward-compatible")
            record = await uow.tool_invocations.create(
                ToolInvocationCreate(
                    tenant_id=tenant.id,
                    agent_id="examples.previous",
                    run_id=None,
                    tool_name="file.read_file",
                    args_ref="artifact://old-args",
                    result_ref="artifact://old-result",
                    status="completed",
                )
            )
            await uow.commit()
        async with storage.uow() as uow:
            loaded = await uow.tool_invocations.get(record.id)
    finally:
        await storage.dispose()

    assert loaded is not None
    assert loaded.approval_id is None
    assert loaded.arguments_hash is None
    assert loaded.execution_state is None


@pytest.mark.skipif(
    not os.environ.get("AGENT_HARNESS_TEST_POSTGRES_DSN"),
    reason="PostgreSQL contract runs only when service smoke provides a DSN.",
)
@pytest.mark.asyncio
async def test_repository_contract_postgresql_service_adapter() -> None:
    """服务 PostgreSQL adapter 必须复用同一 repository seam，验证本地实现没有掩盖后端差异。"""

    # PostgreSQL adapter 必须跑同一批 repository seam。默认跳过是为了让本地 unit gate
    # 不依赖 Docker；service smoke 会注入 DSN 单独执行，完成目标时必须贴出那份证据。
    dsn = os.environ["AGENT_HARNESS_TEST_POSTGRES_DSN"]
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    suffix = uuid4().hex
    idempotency_key = f"pg-idem-{suffix}"
    trace_id = f"trace-pg-idem-{suffix}"

    try:
        async with storage.uow() as uow:
            tenant = await uow.tenants.ensure("default")
            session = await uow.sessions.create(
                SessionCreate(
                    tenant_id=tenant.id,
                    user_id="service-user",
                    agent_id="fake-agent",
                )
            )
            run = await uow.runs.create(
                RunCreate(
                    tenant_id=tenant.id,
                    session_id=session.id,
                    agent_id="fake-agent",
                    idempotency_key=idempotency_key,
                    trace_id=trace_id,
                    input={"profile": "service"},
                )
            )
            await uow.commit()

        async with storage.uow() as uow:
            same_run = await uow.runs.get_by_idempotency_key(
                tenant_id="default",
                session_id=session.id,
                agent_id="fake-agent",
                idempotency_key=idempotency_key,
            )
        assert same_run is not None
        assert same_run.id == run.id
    finally:
        await storage.dispose()


def test_doctor_cli_reports_local_storage_migration_and_eval_status(tmp_path: Path) -> None:
    """operator-facing doctor CLI 应报告临时本地库的迁移、存储与评测目录状态而不触碰默认数据。"""

    # doctor 是 operator-facing seam，所以通过 `python -m agent_harness.cli` 跑真实 CLI。
    # `--storage-dsn` 指向临时库，证明诊断可读 migration 状态，同时不污染 local.yaml 默认库。
    db_path = tmp_path / "agent_harness.db"
    run_migrations(sqlite_dsn(db_path))

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_harness.cli",
            "doctor",
            "--profile",
            "local",
            "--profiles-dir",
            str(PROFILES),
            "--storage-dsn",
            sqlite_dsn(db_path),
        ],
        check=False,
        cwd=ROOT,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert "storage: sqlite" in result.stdout
    assert "migration: 0016_shared_parent_budget_ledger" in result.stdout
    assert "redis: not required" in result.stdout
    assert "eval directory:" in result.stdout


def test_sqlalchemy_session_boundary_scan_has_no_business_leaks() -> None:
    """静态门禁禁止业务入口直接依赖 SQLAlchemy session，强制通过 repository/UoW 保持事务边界。"""

    # 静态扫描只锁业务入口不直接 import Session/AsyncSession；storage adapter 和 migration
    # 可以使用 SQLAlchemy。这个门禁防止后续 API/agent/eval 绕过 repository/UoW。
    assert check_sqlalchemy_session_boundaries() == []
