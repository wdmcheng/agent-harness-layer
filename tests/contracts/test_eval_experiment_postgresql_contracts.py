"""Eval experiment PostgreSQL migration、repository 与降级合同。"""

from __future__ import annotations

import asyncio
import os
from argparse import Namespace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from tests.contracts.test_eval_experiment_storage_contracts import (
    acceptance_create,
    experiment_create,
    split_create,
)


@pytest.mark.skipif(
    not os.environ.get("AGENT_HARNESS_TEST_POSTGRES_DSN"),
    reason="PostgreSQL eval experiment contract runs when service smoke provides a DSN.",
)
@pytest.mark.asyncio
async def test_eval_experiment_postgresql_repository_and_downgrade_contract() -> None:
    """在隔离 PostgreSQL database 验证 repository、rollback 与 empty-only downgrade。"""

    from sqlalchemy.engine import make_url
    from sqlalchemy.ext.asyncio import create_async_engine

    from agent_harness.storage import (
        SQLAlchemyStorage,
        get_current_revision,
        get_head_revision,
        run_migrations,
    )
    from agent_harness.storage.migrations.runner import alembic_config

    base_url = make_url(os.environ["AGENT_HARNESS_TEST_POSTGRES_DSN"])
    database_name = f"agent_harness_eval_experiment_{uuid4().hex}"
    admin_url = base_url.set(database="postgres")
    test_url = base_url.set(database=database_name)
    admin_engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    async with admin_engine.connect() as connection:
        await connection.exec_driver_sql(f'CREATE DATABASE "{database_name}"')
    await admin_engine.dispose()

    dsn = test_url.render_as_string(hide_password=False)

    def downgrade_config() -> Config:
        config = alembic_config(dsn)
        config.cmd_opts = Namespace(x=["allow_empty_evidence_downgrade=true"])
        return config

    storage: SQLAlchemyStorage | None = None
    try:
        await asyncio.to_thread(run_migrations, dsn)
        await asyncio.to_thread(
            command.downgrade,
            downgrade_config(),
            "0008_agent_execution_approval_claims",
        )
        assert (
            await asyncio.to_thread(get_current_revision, dsn)
            == "0008_agent_execution_approval_claims"
        )
        await asyncio.to_thread(run_migrations, dsn)

        storage = SQLAlchemyStorage.from_dsn(dsn)
        async with storage.uow() as uow:
            await uow.tenants.ensure("tenant-a")
            await uow.eval_dataset_splits.create(split_create())
            experiment = await uow.eval_experiments.create(experiment_create())
            claimed = await uow.eval_experiments.claim_execution(
                tenant_id="tenant-a",
                experiment_id=experiment.experiment_id,
                claim_id="postgres-claim",
                expires_at=datetime.now(tz=UTC) + timedelta(seconds=30),
            )
            acceptance = acceptance_create()
            acceptance.experiment_id = experiment.experiment_id
            decision = await uow.harness_acceptance_records.create(acceptance)
            await uow.commit()
        assert claimed is True

        async with storage.uow() as uow:
            split_replay = await uow.eval_dataset_splits.create(split_create())
            experiment_replay = await uow.eval_experiments.create(experiment_create())
            decision_replay = await uow.harness_acceptance_records.create(acceptance)
            marked = await uow.eval_experiments.mark_execution_needs_review(
                tenant_id="tenant-a",
                experiment_id=experiment.experiment_id,
                claim_id="postgres-claim",
                reason_code="eval.experiment.execution_outcome_uncertain",
            )
            await uow.commit()
        assert split_replay.split_id == "split-1"
        assert experiment_replay.experiment_id == experiment.experiment_id
        assert decision_replay.acceptance_id == decision.acceptance_id
        assert marked is True

        rollback_split = split_create().model_copy(update={"split_id": "split-rollback"})
        async with storage.uow() as uow:
            await uow.eval_dataset_splits.create(rollback_split)
        async with storage.uow() as uow:
            assert await uow.eval_dataset_splits.get("tenant-a", "split-rollback") is None

        await storage.dispose()
        storage = None
        with pytest.raises(RuntimeError, match="0011 downgrade refused"):
            await asyncio.to_thread(
                command.downgrade,
                downgrade_config(),
                "0008_agent_execution_approval_claims",
            )
        assert await asyncio.to_thread(get_current_revision, dsn) == get_head_revision()
        storage = SQLAlchemyStorage.from_dsn(dsn)
        async with storage.uow() as uow:
            assert await uow.eval_dataset_splits.get("tenant-a", "split-1") is not None
    finally:
        if storage is not None:
            await storage.dispose()
        admin_engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
        async with admin_engine.connect() as connection:
            await connection.exec_driver_sql(f'DROP DATABASE "{database_name}" WITH (FORCE)')
        await admin_engine.dispose()
