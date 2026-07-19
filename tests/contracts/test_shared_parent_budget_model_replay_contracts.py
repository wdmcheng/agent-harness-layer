"""Model durable result 与当前 snapshot 漂移的重放合同。"""

# 场景复用统一 frozen-root 与 model provider 夹具。
# ruff: noqa: F403, F405
from tests.contracts.test_shared_parent_budget_invocation_contracts import *


@pytest.mark.asyncio
async def test_exact_model_replay_precedes_current_snapshot_integrity(
    tmp_path: Path,
) -> None:
    """已有可信结果按 durable identity 重放，不受后续 snapshot 损坏影响。"""

    dsn = f"sqlite+aiosqlite:///{tmp_path / 'model-replay-corrupt-snapshot.sqlite3'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    sink = LocalJsonlEventSink(tmp_path / "model-replay-corrupt-snapshot-events.jsonl")
    provider = CountingFakeModelProvider()
    service = model_service(storage=storage, sink=sink, provider=provider)
    run_id = await seed_managed_root(storage)
    request = model_request()
    try:
        first = await service.complete(
            request,
            context=context(run_id),
            usage_call_id="usage-model-replay-corrupt-snapshot",
        )
        async with storage.uow() as uow:
            ledger = await uow.session.get(
                ParentBudgetLedgerModel,
                ("tenant-a", run_id),
            )
            assert ledger is not None
            snapshot = dict(ledger.snapshot_json)
            snapshot["catalog_version"] = "catalog-corrupted-after-result"
            ledger.snapshot_json = snapshot
            await uow.commit()

        replayed = await service.complete(
            request,
            context=context(run_id),
            usage_call_id="usage-model-replay-corrupt-snapshot",
        )
        with pytest.raises(BudgetOperationConflict):
            await service.complete(
                request.model_copy(update={"prompt": "changed after durable result"}),
                context=context(run_id),
                usage_call_id="usage-model-replay-corrupt-snapshot",
            )

        assert replayed == first
        assert provider.calls == 1
    finally:
        await storage.dispose()
