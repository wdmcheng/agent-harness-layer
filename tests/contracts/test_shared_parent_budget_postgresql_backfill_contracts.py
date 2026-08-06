"""真实 PostgreSQL 0016 backfill 与 evidence-aware downgrade 合同。"""

# 所有场景共享同一真实 PostgreSQL isolated database 与 ledger 夹具。
# ruff: noqa: F403, F405
from tests.contracts.test_shared_parent_budget_postgresql_contracts import *


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "backfill_case",
    [
        "valid",
        "identity-mismatch",
        "unused-target-incomplete",
        "missing-source",
        "source-conflict",
        "history-conflict",
        "null-input-price",
        "bool-output-price",
        "negative-input-price",
        "nan-output-price",
        "infinity-input-price",
        "null-embedding-price",
        "bool-embedding-price",
        "negative-embedding-price",
        "nan-embedding-price",
        "infinity-embedding-price",
    ],
)
async def test_postgresql_0016_backfill_and_evidence_aware_downgrade(
    backfill_case: str,
) -> None:
    """真实 PostgreSQL 逐值回填 direct bundle，并拒绝删除已生成的 0016 evidence。"""

    async with isolated_database(f"shared_budget_migration_backfill_{backfill_case}") as dsn:
        await asyncio.to_thread(run_migrations, dsn, "0015_agent_delegation")
        identity_token_bound = 6 if backfill_case == "identity-mismatch" else 5
        snapshot: dict[str, Any] = {
            "owner": {
                "agent_id": "agent-a",
                "root_run_id": "root-a",
                "delegation_targets": [],
                "max_tokens_per_run": 100,
                "max_cost_usd_per_run": None,
                "cost_enabled": False,
            },
            "registry_version": "registry-v1",
            "config_version": "config-v1",
            "catalog_version": "catalog-v1",
            "agents": {
                "agent-a": {
                    "agent_id": "agent-a",
                    "descriptor_version": "agent-a-v1",
                    "model_policy": {
                        "provider": "fake",
                        "default_model": "fake-basic",
                        "fallback_models": [],
                    },
                    "target_budget": {
                        "max_tokens_per_run": 100,
                        "max_cost_usd_per_run": None,
                    },
                    "routes": [
                        {
                            "usage_kind": "model",
                            "provider": "fake",
                            "model": "fake-basic",
                            "price_source_ref": "catalog:fake",
                            "price_source_version": "v1",
                            "input_token_price_usd": "0",
                            "output_token_price_usd": "0",
                        }
                    ],
                }
            },
        }
        if backfill_case == "unused-target-incomplete":
            owner = cast(dict[str, Any], snapshot["owner"])
            owner["delegation_targets"] = ["agent-b"]
            agents = cast(dict[str, Any], snapshot["agents"])
            agents["agent-b"] = {
                "agent_id": "agent-b",
                "descriptor_version": "agent-b-v1",
                "model_policy": {
                    "provider": "fake",
                    "default_model": "fake-basic",
                    "fallback_models": ["fake-fallback"],
                },
                "target_budget": {
                    "max_tokens_per_run": 20,
                    "max_cost_usd_per_run": None,
                },
                "routes": [
                    {
                        "usage_kind": "model",
                        "provider": "fake",
                        "model": "fake-basic",
                        "price_source_ref": "catalog:fake",
                        "price_source_version": "v1",
                        "input_token_price_usd": "0",
                        "output_token_price_usd": "0",
                    }
                ],
            }
        price_case = backfill_case.endswith("-price")
        if price_case:
            owner = cast(dict[str, Any], snapshot["owner"])
            owner["max_cost_usd_per_run"] = "10"
            owner["cost_enabled"] = True
            agent = cast(dict[str, Any], cast(dict[str, Any], snapshot["agents"])["agent-a"])
            cast(dict[str, Any], agent["target_budget"])["max_cost_usd_per_run"] = "10"
            invalid_price: object = {
                "null-input-price": None,
                "bool-output-price": True,
                "negative-input-price": "-1",
                "nan-output-price": "NaN",
                "infinity-input-price": "Infinity",
                "null-embedding-price": None,
                "bool-embedding-price": True,
                "negative-embedding-price": "-1",
                "nan-embedding-price": "NaN",
                "infinity-embedding-price": "Infinity",
            }[backfill_case]
            routes = cast(list[dict[str, Any]], agent["routes"])
            if "embedding" in backfill_case:
                routes.append(
                    {
                        "usage_kind": "embedding",
                        "provider": "local",
                        "model": "mock-small",
                        "price_source_ref": "catalog:local",
                        "price_source_version": "v1",
                        "input_token_price_usd": invalid_price,
                    }
                )
            else:
                field = (
                    "output_token_price_usd"
                    if backfill_case in {"bool-output-price", "nan-output-price"}
                    else "input_token_price_usd"
                )
                routes[0][field] = invalid_price
        operation = OperationIdentity.from_semantic_request(
            tenant_id="tenant-a",
            fingerprint_key=b"legacy-test-key",
            fingerprint_key_version="legacy-key-v1",
            ownership_kind="direct",
            run_id="root-a",
            agent_id="agent-a",
            delegation_claim_id=None,
            usage_kind="model",
            operation_slot="turn:1:model",
            semantic_request={"prompt_ref": "legacy-request"},
            tree_snapshot_id="snapshot:legacy-pg",
            agent_sub_snapshot_id="snapshot:legacy-pg:agent-a",
            provider="fake",
            model="fake-basic",
            price_source_ref="catalog:fake",
            price_source_version="v1",
            cache_key_digest=None,
            cost_enabled=price_case,
            trusted_token_bound=identity_token_bound,
            trusted_cost_bound=Decimal("1") if price_case else None,
        ).to_payload()
        result = {"outcome": "completed", "evidence": {"provider_called": True}}
        bundle: dict[str, Any] = {
            "ledger": {
                "token_limit": 100,
                "cost_limit": "10" if price_case else None,
                "cost_enabled": price_case,
                "token_impact": 4,
                "cost_impact": "0.5" if price_case else "0",
                "state": "active",
                "version": 1,
                "registry_version": "registry-v1",
                "config_version": "config-v1",
                "catalog_version": "catalog-v1",
                "snapshot_id": "snapshot:legacy-pg",
                "snapshot_hash": canonical_hash(snapshot),
                "snapshot": snapshot,
            },
            "claims": [
                {
                    "id": "claim-a",
                    "operation_kind": "direct",
                    "usage_call_id": "usage-a",
                    "delegation_id": None,
                    "run_id": "root-a",
                    "agent_id": "agent-a",
                    "usage_kind": "model",
                    "identity_json": operation,
                    "request_hash": None,
                    "reserved_tokens": 5,
                    "reserved_cost": "1" if price_case else None,
                    "actual_tokens": 4,
                    "actual_cost": "0.5" if price_case else None,
                    "token_impact": 4,
                    "cost_impact": "0.5" if price_case else "0",
                    "state": "settled",
                    "side_effect_state": "result_committed",
                    "result_json": result,
                    "backfill_source": "legacy_settled",
                }
            ],
            "allocations": [],
        }
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
                    "input_json) values ("
                    "'root-a','tenant-a','session-a','agent-a','running','trace-a',"
                    "cast('{}' as jsonb))"
                )
            )
            await connection.execute(
                text(
                    "insert into run_event_capacity(run_id,tenant_id,highest_persisted_seq,"
                    "outstanding_reserved_event_count,terminal_reservation) "
                    "values ('root-a','tenant-a',2,0,1)"
                )
            )
            await connection.execute(
                text(
                    "insert into run_evidence_outbox(id,tenant_id,run_id,usage_call_id,event_id,"
                    "operation_kind,state,result_json,reserved_event_count) values ("
                    "'outbox-a','tenant-a','root-a','usage-a','usage-event-a','model_usage',"
                    "'published',cast(:result as jsonb),2)"
                ),
                {"result": json.dumps(result)},
            )
            if backfill_case == "missing-source":
                await connection.execute(
                    text(
                        "insert into checkpoints(id,tenant_id,run_id,sequence,resume_token,"
                        "state_json) values ('checkpoint-a','tenant-a','root-a',1,'resume-a',"
                        "cast(:state as jsonb))"
                    ),
                    {"state": json.dumps({"shared_budget_backfill_v1": bundle})},
                )
            else:
                await seed_postgresql_backfill_records(
                    connection,
                    tenant_id="tenant-a",
                    run_id="root-a",
                    bundle=bundle,
                    prefix="checkpoint-a",
                )
                if backfill_case == "source-conflict":
                    await connection.execute(
                        text(
                            "update checkpoints set state_json=cast(:state as jsonb) "
                            "where id='checkpoint-a-source'"
                        ),
                        {
                            "state": json.dumps(
                                {"shared_budget_source_v1": {"source_version": "tampered"}}
                            )
                        },
                    )
                if backfill_case == "history-conflict":
                    await connection.execute(
                        text(
                            "update checkpoints set state_json=cast(:state as jsonb) "
                            "where id='checkpoint-a-history'"
                        ),
                        {
                            "state": json.dumps(
                                {
                                    "shared_budget_history_v1": {
                                        "history_version": "shared-budget-history-v1",
                                        "registry_version": "current-config-v2",
                                    }
                                }
                            )
                        },
                    )
        await engine.dispose()

        if backfill_case != "valid":
            expected = (
                "independent source is missing"
                if backfill_case == "missing-source"
                else "independent source conflicts with bundle"
                if backfill_case == "source-conflict"
                else "versioned history is invalid"
                if backfill_case == "history-conflict"
                else "target sub-snapshot is incomplete"
                if backfill_case == "unused-target-incomplete" or price_case
                else "direct identity is invalid"
            )
            with pytest.raises(RuntimeError, match=expected):
                await asyncio.to_thread(run_migrations, dsn)
            assert await asyncio.to_thread(get_current_revision, dsn) == "0015_agent_delegation"
            return

        await asyncio.to_thread(run_migrations, dsn)
        engine = create_async_engine(dsn)
        async with engine.connect() as connection:
            ledger = (
                await connection.execute(
                    text(
                        "select budget_owner_run_id,token_impact,state,snapshot_id "
                        "from parent_budget_ledgers"
                    )
                )
            ).one()
            claim = (
                await connection.execute(
                    text(
                        "select operation_kind,usage_call_id,token_impact,state,backfill_source "
                        "from budget_operation_claims"
                    )
                )
            ).one()
        await engine.dispose()
        assert tuple(ledger) == ("root-a", 4, "active", "snapshot:legacy-pg")
        assert tuple(claim) == ("direct", "usage-a", 4, "settled", "legacy_settled")

        with pytest.raises(RuntimeError, match="shared budget evidence exists"):
            await asyncio.to_thread(
                command.downgrade,
                migration_config(dsn, x_args=["allow_empty_evidence_downgrade=true"]),
                "0015_agent_delegation",
            )
        assert await asyncio.to_thread(get_current_revision, dsn) == ("0018_model_tool_loop_state")
