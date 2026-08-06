"""Agent 委派迁移受控降级合同测试。"""

from __future__ import annotations

from tests.contracts.test_agent_delegation_storage_contracts import (
    Path as Path,
)
from tests.contracts.test_agent_delegation_storage_contracts import (
    SQLAlchemyStorage as SQLAlchemyStorage,
)
from tests.contracts.test_agent_delegation_storage_contracts import (
    _claim as _claim,
)
from tests.contracts.test_agent_delegation_storage_contracts import (
    _create_child_relation as _create_child_relation,
)
from tests.contracts.test_agent_delegation_storage_contracts import (
    _create_parent as _create_parent,
)
from tests.contracts.test_agent_delegation_storage_contracts import (
    asyncio as asyncio,
)
from tests.contracts.test_agent_delegation_storage_contracts import (
    command as command,
)
from tests.contracts.test_agent_delegation_storage_contracts import (
    migration_config as migration_config,
)
from tests.contracts.test_agent_delegation_storage_contracts import (
    pytest as pytest,
)
from tests.contracts.test_agent_delegation_storage_contracts import (
    run_migrations as run_migrations,
)
from tests.contracts.test_agent_delegation_storage_contracts import (
    sqlite3 as sqlite3,
)
from tests.contracts.test_agent_delegation_storage_contracts import (
    sqlite_dsn as sqlite_dsn,
)


@pytest.mark.parametrize(
    "x_args",
    [
        [],
        ["allow_empty_evidence_downgrade=false"],
        ["allow_empty_evidence_downgrade=True"],
        ["allow_empty_evidence_downgrade=true", "allow_empty_evidence_downgrade=true"],
        ["allow_empty_evidence_downgrade=true", "unrelated_flag=1"],
    ],
)
def test_0015_downgrade_requires_exact_opt_in(tmp_path: Path, x_args: list[str]) -> None:
    """受控降级只接受唯一且精确的显式开关，模糊、重复或夹带参数都不得绕过保护。"""

    path = tmp_path / f"delegation-downgrade-{len(x_args)}-{hash(tuple(x_args))}.db"
    run_migrations(sqlite_dsn(path))

    with pytest.raises(RuntimeError, match="explicit opt-in"):
        command.downgrade(
            migration_config(sqlite_dsn(path), x_args=x_args),
            "0014_run_evidence_outbox",
        )
    with sqlite3.connect(path) as connection:
        assert connection.execute("select version_num from alembic_version").fetchone() == (
            "0018_model_tool_loop_state",
        )


def test_0015_empty_database_downgrades_with_exact_opt_in(tmp_path: Path) -> None:
    """没有任何委派证据的空库在明确确认后允许降级，验证可恢复性并限定安全前提。"""

    path = tmp_path / "delegation-empty-downgrade.db"
    run_migrations(sqlite_dsn(path))

    command.downgrade(
        migration_config(
            sqlite_dsn(path),
            x_args=["allow_empty_evidence_downgrade=true"],
        ),
        "0014_run_evidence_outbox",
    )
    with sqlite3.connect(path) as connection:
        assert connection.execute("select version_num from alembic_version").fetchone() == (
            "0014_run_evidence_outbox",
        )


def test_0015_any_claim_blocks_exact_opt_in_downgrade(tmp_path: Path) -> None:
    """只要已存在委派 claim，即使传入确认开关也禁止降级，防止删失耐久业务事实。"""

    path = tmp_path / "delegation-non-empty-downgrade.db"
    run_migrations(sqlite_dsn(path))

    async def seed() -> None:
        """创建最小持久化 claim，作为降级保护门禁必须检测到的真实证据。"""

        storage = SQLAlchemyStorage.from_dsn(sqlite_dsn(path))
        try:
            parent_run_id = await _create_parent(storage)
            async with storage.uow() as uow:
                await uow.delegations.claim_and_reserve(_claim(parent_run_id))
                await uow.commit()
        finally:
            await storage.dispose()

    asyncio.run(seed())

    with pytest.raises(RuntimeError, match="evidence exists"):
        command.downgrade(
            migration_config(
                sqlite_dsn(path),
                x_args=["allow_empty_evidence_downgrade=true"],
            ),
            "0014_run_evidence_outbox",
        )
    with sqlite3.connect(path) as connection:
        assert connection.execute("select count(*) from agent_delegations").fetchone() == (1,)
        assert connection.execute("select version_num from alembic_version").fetchone() == (
            "0018_model_tool_loop_state",
        )


def test_0015_run_relation_alone_blocks_exact_opt_in_downgrade(tmp_path: Path) -> None:
    """独立 run 关系也是 0015 证据，不能因三个新表为空而被删除能力。"""

    path = tmp_path / "delegation-relation-only-downgrade.db"
    run_migrations(sqlite_dsn(path))

    async def seed() -> tuple[str, str]:
        """只建立父子运行关系而不写新表，用于证明旧关系同样属于不可降级证据。"""

        storage = SQLAlchemyStorage.from_dsn(sqlite_dsn(path))
        try:
            parent_run_id = await _create_parent(storage)
            child_run_id = await _create_child_relation(storage, parent_run_id=parent_run_id)
            return parent_run_id, child_run_id
        finally:
            await storage.dispose()

    parent_run_id, child_run_id = asyncio.run(seed())
    with sqlite3.connect(path) as connection:
        assert connection.execute("select count(*) from agent_delegations").fetchone() == (0,)
        assert connection.execute(
            "select count(*) from delegation_budget_reservations"
        ).fetchone() == (0,)
        assert connection.execute("select count(*) from delegation_aggregates").fetchone() == (0,)

    with pytest.raises(RuntimeError, match="evidence exists"):
        command.downgrade(
            migration_config(
                sqlite_dsn(path),
                x_args=["allow_empty_evidence_downgrade=true"],
            ),
            "0014_run_evidence_outbox",
        )
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "select parent_run_id from agent_runs where id = ?", (child_run_id,)
        ).fetchone() == (parent_run_id,)
        assert connection.execute("select version_num from alembic_version").fetchone() == (
            "0018_model_tool_loop_state",
        )
