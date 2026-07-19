"""真实 PostgreSQL delegation 四态、actual-over 与 downgrade 合同。"""

# PostgreSQL 场景的数据准备较长，独立到 support 后只在此保留参数与验收断言入口。
# ruff: noqa: F403, F405
from tests.contracts.shared_parent_budget_postgresql_delegation_test_support import (
    assert_postgresql_0016_delegation_actual_over_and_released_proof,
)
from tests.contracts.test_shared_parent_budget_postgresql_contracts import *


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "accepted"),
    [
        ("settled-actual-over", True),
        ("released-valid", True),
        ("released-missing-lifecycle", False),
        ("released-with-side-effects", False),
        ("released-terminal-with-side-effects", False),
        ("released-terminal-canonical-child-event", False),
        ("released-terminal-missing-reservation", False),
        ("released-checkpoint-missing-reservation", False),
        ("released-checkpoint-missing-frozen-target", False),
    ],
)
async def test_postgresql_0016_delegation_actual_over_and_released_proof(
    mode: str,
    accepted: bool,
) -> None:
    await assert_postgresql_0016_delegation_actual_over_and_released_proof(mode, accepted)


@pytest.mark.asyncio
async def test_postgresql_0016_empty_downgrade_with_exact_opt_in() -> None:
    """真实 PostgreSQL 的空 evidence downgrade 只在精确 opt-in 下成功。"""

    async with isolated_database("shared_budget_empty_downgrade") as dsn:
        await asyncio.to_thread(run_migrations, dsn)
        await asyncio.to_thread(
            command.downgrade,
            migration_config(dsn, x_args=["allow_empty_evidence_downgrade=true"]),
            "0015_agent_delegation",
        )
        assert await asyncio.to_thread(get_current_revision, dsn) == "0015_agent_delegation"
