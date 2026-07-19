"""真实 PostgreSQL 竞争、catalog 与 replay integrity 合同。"""

from typing import Any

from sqlalchemy.exc import IntegrityError

# 所有场景共享同一真实 PostgreSQL isolated database 与 ledger 夹具。
# ruff: noqa: F403, F405
from tests.contracts.test_shared_parent_budget_postgresql_contracts import *

from agent_harness.storage.delegation_repositories import DelegationBudgetExceeded


@pytest.mark.asyncio
async def test_postgresql_mixed_direct_delegation_race_has_one_lock_order() -> None:
    """重复 mixed race，禁止 parent/ledger 反向持锁偶发 PostgreSQL deadlock。"""

    async with isolated_database("shared_budget_mixed_lock_order") as dsn:
        await asyncio.to_thread(run_migrations, dsn)
        storage = SQLAlchemyStorage(dsn)
        try:
            for index in range(20):
                suffix = f"pg-mixed-{index}"
                root = await create_root(storage, suffix=suffix)
                async with storage.uow() as uow:
                    snapshot = await uow.shared_budget.get_tree_snapshot("tenant-a", root)
                assert snapshot is not None

                async def compete_direct(root_id: str, case_suffix: str) -> str:
                    """在独立 PostgreSQL 事务中竞争 direct claim。

                    返回结果便于检测锁顺序下是否仍只有一个胜者。
                    """

                    try:
                        async with storage.uow() as uow:
                            await uow.shared_budget.claim_direct(
                                direct_claim(
                                    root_id=root_id,
                                    usage_call_id=f"usage-{case_suffix}",
                                    fingerprint=f"request-{case_suffix}",
                                    token_bound=60,
                                    cost_bound=Decimal("6.00"),
                                )
                            )
                            await uow.commit()
                        return "committed"
                    except BudgetReservationRejected:
                        return "rejected"

                async def compete_delegation(
                    root_id: str,
                    frozen_snapshot: dict[str, Any],
                    case_suffix: str,
                    case_index: int,
                ) -> str:
                    """以同一冻结目录竞争 delegation 预约，验证其锁顺序可与 direct 路径安全交错。"""

                    try:
                        async with storage.uow() as uow:
                            await uow.delegations.claim_and_reserve(
                                delegation_claim(
                                    root_id=root_id,
                                    snapshot=frozen_snapshot,
                                    key=f"delegation-{case_suffix}",
                                    request_hash=f"{case_index:064x}",
                                    trace_id=f"trace-{case_suffix}",
                                    requested_tokens=100,
                                    requested_cost=10.0,
                                )
                            )
                            await uow.commit()
                        return "committed"
                    except DelegationBudgetExceeded:
                        return "rejected"

                outcomes = await asyncio.gather(
                    compete_direct(root, suffix),
                    compete_delegation(root, snapshot, suffix, index),
                )
                assert sorted(outcomes) == ["committed", "rejected"]
        finally:
            await storage.dispose()


@pytest.mark.asyncio
async def test_postgresql_token_cost_race_commits_only_safe_combination() -> None:
    """两个进程级 session 同时竞争 token/cost 时只允许一个提交。"""

    async with isolated_database("shared_budget_token_cost") as dsn:
        await asyncio.to_thread(run_migrations, dsn)
        storage = SQLAlchemyStorage(dsn)
        try:
            root = await create_root(storage, suffix="pg-token-cost")

            async def compete(suffix: str) -> str:
                """发起一次 token/cost 组合预约，将预算拒绝显式归类以比较并发事务结果。"""

                async with storage.uow() as uow:
                    try:
                        await uow.shared_budget.claim_direct(
                            direct_claim(
                                root_id=root,
                                usage_call_id=f"usage-{suffix}",
                                fingerprint=f"request-{suffix}",
                                token_bound=60,
                                cost_bound=Decimal("6.00"),
                            )
                        )
                    except BudgetReservationRejected:
                        return "rejected"
                    await uow.commit()
                    return "committed"

            outcomes = await asyncio.gather(compete("a"), compete("b"))
            assert sorted(outcomes) == ["committed", "rejected"]
            async with storage.uow() as uow:
                ledger = await uow.shared_budget.get_ledger("tenant-a", root)
            assert ledger is not None
            assert ledger.token_impact == 60
            assert ledger.cost_impact == Decimal("6.00000000")
        finally:
            await storage.dispose()


@pytest.mark.asyncio
async def test_postgresql_same_key_unique_race_converges_to_exact_replay() -> None:
    """同 stable key/identity 的 unique race 只扣一次并返回一次 replay。"""

    async with isolated_database("shared_budget_same_key") as dsn:
        await asyncio.to_thread(run_migrations, dsn)
        storage = SQLAlchemyStorage(dsn)
        try:
            root = await create_root(storage, suffix="pg-same-key")
            claim = direct_claim(
                root_id=root,
                usage_call_id="same-usage",
                fingerprint="same-request",
                token_bound=40,
                cost_bound=Decimal("3.00"),
            )

            async def compete() -> bool:
                """用完全相同的 stable key 并发创建 claim，返回 replay 标记验证唯一约束收敛。"""

                async with storage.uow() as uow:
                    result = await uow.shared_budget.claim_direct(claim)
                    await uow.commit()
                    return result.replayed

            replayed = await asyncio.gather(compete(), compete())
            assert sorted(replayed) == [False, True]
            async with storage.uow() as uow:
                ledger = await uow.shared_budget.get_ledger("tenant-a", root)
            assert ledger is not None
            assert ledger.token_impact == 40
            assert ledger.cost_impact == Decimal("3.00000000")
        finally:
            await storage.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "target-missing",
        "agent-mismatch",
        "budget-over-owner",
        "route-price-missing",
        "fallback-route-missing",
    ],
)
async def test_postgresql_ledger_creation_rejects_incomplete_target_catalog(
    case: str,
) -> None:
    """真实 PostgreSQL 与 SQLite 在首次 catalog 完整性上逐值一致。"""

    async with isolated_database(f"shared_budget_catalog_{case.replace('-', '_')}") as dsn:
        await asyncio.to_thread(run_migrations, dsn)
        storage = SQLAlchemyStorage(dsn)
        try:
            case_code = {
                "target-missing": "tm",
                "agent-mismatch": "am",
                "budget-over-owner": "bo",
                "route-price-missing": "rp",
                "fallback-route-missing": "fr",
            }[case]
            suffix = f"pgcat-{case_code}"
            baseline = await create_root(storage, suffix=suffix)
            async with storage.uow() as uow:
                run = await uow.runs.create(
                    RunCreate(
                        tenant_id="tenant-a",
                        session_id=f"session-{suffix}",
                        agent_id="agent-a",
                        trace_id=f"trace-pg-invalid-catalog-{case}",
                    )
                )
                baseline_snapshot = await uow.shared_budget.get_tree_snapshot("tenant-a", baseline)
                assert baseline_snapshot is not None
                snapshot = corrupt_tree_catalog(baseline_snapshot, case)
                owner = snapshot["owner"]
                assert isinstance(owner, dict)
                owner["root_run_id"] = run.id
                with pytest.raises(BudgetReservationRejected) as rejected:
                    await uow.shared_budget.create_ledger(
                        LedgerCreate(
                            tenant_id="tenant-a",
                            budget_owner_run_id=run.id,
                            token_limit=100,
                            cost_limit=Decimal("10.00"),
                            registry_version="registry-v1",
                            config_version="config-v1",
                            catalog_version="catalog-v1",
                            snapshot_id=f"snapshot:{run.id}",
                            snapshot=snapshot,
                        )
                    )
                assert rejected.value.reason == "snapshot_invalid"
                assert await uow.shared_budget.get_ledger("tenant-a", run.id) is None
                await uow.commit()
        finally:
            await storage.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("pending_kind", ["root-queue", "child-queue", "approval-enqueue"])
async def test_postgresql_0016_rejects_terminal_tree_with_pending_queue_recovery(
    pending_kind: str,
) -> None:
    """真实 PostgreSQL 必须在 DDL 前拒绝 terminal tree 的 durable queue 待办。"""

    async with isolated_database(f"shared_budget_queue_{pending_kind.replace('-', '_')}") as dsn:
        await asyncio.to_thread(run_migrations, dsn, "0015_agent_delegation")
        engine = create_async_engine(dsn)
        async with engine.begin() as connection:
            await connection.execute(
                text("insert into tenants(id,display_name) values ('tenant-a','tenant-a')")
            )
            await connection.execute(
                text(
                    "insert into sessions(id,tenant_id,user_id,metadata_json) "
                    "values ('session-a','tenant-a','user-a',cast('{}' as jsonb))"
                )
            )
            await connection.execute(
                text(
                    "insert into run_trace_bindings(trace_id,tenant_id,root_run_id) "
                    "values ('trace-a','tenant-a','root-a')"
                )
            )
            await connection.execute(
                text(
                    "insert into agent_runs(id,tenant_id,session_id,agent_id,status,trace_id,"
                    "input_json,queue_operation_id,queue_enqueue_state) values ("
                    "'root-a','tenant-a','session-a','agent-a','completed','trace-a',"
                    "cast('{}' as jsonb),:operation_id,:enqueue_state)"
                ),
                {
                    "operation_id": (
                        "run:root-a:execute" if pending_kind == "root-queue" else None
                    ),
                    "enqueue_state": ("enqueue_pending" if pending_kind == "root-queue" else None),
                },
            )
            run_ids = ["root-a"]
            if pending_kind == "child-queue":
                await connection.execute(
                    text(
                        "insert into agent_runs(id,tenant_id,session_id,agent_id,status,trace_id,"
                        "input_json,parent_run_id,queue_operation_id,queue_enqueue_state) values ("
                        "'child-a','tenant-a','session-a','agent-b','completed','trace-a',"
                        "cast('{}' as jsonb),'root-a','run:child-a:execute','enqueue_pending')"
                    )
                )
                await connection.execute(
                    text(
                        "insert into agent_delegations("
                        "id,tenant_id,parent_run_id,child_run_id,source_agent_id,target_agent_id,"
                        "idempotency_key,request_hash,budget_intent,child_input_json,identity_json,"
                        "trace_id,status,error_json,event_operation_kind,event_registry_version,"
                        "reserved_event_count) values ("
                        "'delegation-a','tenant-a','root-a','child-a','agent-a','agent-b',"
                        "'child-queue-key',:request_hash,'inherit_parent',cast('{}' as jsonb),"
                        "cast('{}' as jsonb),'trace-a','completed',cast('null' as jsonb),"
                        "'delegation','v1',3)"
                    ),
                    {"request_hash": "a" * 64},
                )
                run_ids.append("child-a")
            for sequence, run_id in enumerate(run_ids, start=1):
                await connection.execute(
                    text(
                        "insert into run_event_capacity(run_id,tenant_id,highest_persisted_seq,"
                        "outstanding_reserved_event_count,terminal_reservation) values ("
                        ":run_id,'tenant-a',1,0,0)"
                    ),
                    {"run_id": run_id},
                )
                await connection.execute(
                    text(
                        "insert into canonical_events(id,tenant_id,run_id,stream_id,event_type,"
                        "seq,terminal,visibility,trace_id,record_scope,envelope_json) values ("
                        ":event_id,'tenant-a',:run_id,:stream_id,'run.completed',1,true,"
                        "'public','trace-a','run',cast('{}' as jsonb))"
                    ),
                    {
                        "event_id": f"terminal-{run_id}",
                        "run_id": run_id,
                        "stream_id": f"stream-{sequence}",
                    },
                )
            if pending_kind == "approval-enqueue":
                await connection.execute(
                    text(
                        "insert into approvals(id,tenant_id,run_id,agent_id,action,resource,"
                        "reason,status,trace_id,metadata_json,resolution_state,"
                        "resolution_operation_id,resolution_enqueue_state) values ("
                        "'approval-a','tenant-a','root-a','agent-a','tool.call','tool:a',"
                        "'pending','approved','trace-a',cast('{}' as jsonb),'completed',"
                        "'approval:resolve','enqueue_pending')"
                    )
                )
        await engine.dispose()

        with pytest.raises(RuntimeError, match="pending queue recovery|pending approval recovery"):
            await asyncio.to_thread(run_migrations, dsn)
        assert get_current_revision(dsn) == "0015_agent_delegation"
        engine = create_async_engine(dsn)
        async with engine.connect() as connection:
            table_name = await connection.scalar(
                text("select to_regclass('parent_budget_ledgers')")
            )
        await engine.dispose()
        assert table_name is None


@pytest.mark.asyncio
async def test_postgresql_replay_rejects_corrupted_persisted_identity_detail() -> None:
    """PostgreSQL exact replay 也必须重算 JSON hash 并绑定 denormalized detail。"""

    async with isolated_database("shared_budget_identity_integrity") as dsn:
        await asyncio.to_thread(run_migrations, dsn)
        storage = SQLAlchemyStorage(dsn)
        try:
            root = await create_root(storage, suffix="pg-identity-integrity")
            claim = direct_claim(
                root_id=root,
                usage_call_id="usage-pg-identity-integrity",
                fingerprint="request-pg-identity-integrity",
                token_bound=40,
                cost_bound=Decimal("3.00"),
            )
            async with storage.uow() as uow:
                await uow.shared_budget.claim_direct(claim)
                await uow.commit()
            with pytest.raises(IntegrityError):
                async with storage.uow() as uow:
                    model = await uow.session.scalar(
                        select(BudgetOperationClaimModel).where(
                            BudgetOperationClaimModel.usage_call_id == "usage-pg-identity-integrity"
                        )
                    )
                    assert model is not None
                    model.identity_json = {}
                    await uow.commit()
            for field in ("source_agent_id", "target_agent_id", "target_route_catalog_digest"):
                with pytest.raises(IntegrityError):
                    async with storage.uow() as uow:
                        model = await uow.session.scalar(
                            select(BudgetOperationClaimModel).where(
                                BudgetOperationClaimModel.usage_call_id
                                == "usage-pg-identity-integrity"
                            )
                        )
                        assert model is not None
                        corrupted = dict(model.identity_json)
                        corrupted[field] = "forged-delegation-only-value"
                        model.identity_json = corrupted
                        await uow.commit()
            async with storage.uow() as uow:
                model = await uow.session.scalar(
                    select(BudgetOperationClaimModel).where(
                        BudgetOperationClaimModel.usage_call_id == "usage-pg-identity-integrity"
                    )
                )
                assert model is not None and model.identity_json is not None
                corrupted = dict(model.identity_json)
                corrupted["provider"] = "tampered-provider"
                model.identity_json = corrupted
                await uow.commit()
            async with storage.uow() as uow:
                with pytest.raises(BudgetOperationConflict):
                    await uow.shared_budget.preflight_direct(claim)
                ledger = await uow.shared_budget.get_ledger("tenant-a", root)
            assert ledger is not None and ledger.token_impact == 40
        finally:
            await storage.dispose()


@pytest.mark.asyncio
async def test_postgresql_atomic_delegation_requires_explicit_frozen_edge() -> None:
    """PostgreSQL 原子 UoW 不得把存在的 target sub-snapshot 当作授权 edge。"""

    async with isolated_database("shared_budget_explicit_edge") as dsn:
        await asyncio.to_thread(run_migrations, dsn)
        storage = SQLAlchemyStorage(dsn)
        try:
            root = await create_root(storage, suffix="pg-explicit-edge")
            async with storage.uow() as uow:
                ledger = await uow.session.get(ParentBudgetLedgerModel, ("tenant-a", root))
                assert ledger is not None
                snapshot = dict(ledger.snapshot_json)
                owner = dict(snapshot["owner"])
                owner["delegation_targets"] = []
                snapshot["owner"] = owner
                ledger.snapshot_json = snapshot
                ledger.snapshot_hash = hashlib.sha256(
                    json.dumps(
                        snapshot,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    ).encode("utf-8")
                ).hexdigest()
                await uow.commit()
            async with storage.uow() as uow:
                with pytest.raises(DelegationStorageConflict, match="delegation.execution_failed"):
                    await uow.delegations.claim_and_reserve(
                        delegation_claim(
                            root_id=root,
                            snapshot=snapshot,
                            key="pg-explicit-edge",
                            request_hash="e" * 64,
                            trace_id="trace-pg-explicit-edge",
                            requested_tokens=20,
                            requested_cost=1.0,
                        )
                    )
                relation = await uow.session.scalar(
                    select(AgentDelegationModel).where(
                        AgentDelegationModel.parent_run_id == root,
                        AgentDelegationModel.idempotency_key == "pg-explicit-edge",
                    )
                )
            assert relation is None
        finally:
            await storage.dispose()
