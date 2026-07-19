"""真实 PostgreSQL delegation 四态与 actual-over 场景执行器。"""

# 所有场景共享同一真实 PostgreSQL isolated database 与 ledger 夹具。
# ruff: noqa: F403, F405
from tests.contracts.test_shared_parent_budget_postgresql_contracts import *


async def assert_postgresql_0016_delegation_actual_over_and_released_proof(
    mode: str,
    accepted: bool,
) -> None:
    """真实 PostgreSQL 与 SQLite 一致执行 actual-over 与 released 安全矩阵。"""

    async with isolated_database(f"shared_budget_delegation_{mode.replace('-', '_')}") as dsn:
        await asyncio.to_thread(run_migrations, dsn, "0015_agent_delegation")
        actual_over = mode == "settled-actual-over"
        released_with_side_effects = mode == "released-with-side-effects"
        terminal_released_with_side_effects = mode == "released-terminal-with-side-effects"
        terminal_canonical_child_event = mode == "released-terminal-canonical-child-event"
        terminal_missing_reservation = mode == "released-terminal-missing-reservation"
        checkpoint_missing_reservation = mode == "released-checkpoint-missing-reservation"
        checkpoint_missing_frozen_target = mode == "released-checkpoint-missing-frozen-target"
        missing_reservation = terminal_missing_reservation or checkpoint_missing_reservation
        terminal_case = (
            terminal_released_with_side_effects
            or terminal_canonical_child_event
            or terminal_missing_reservation
        )
        any_released_side_effects = (
            released_with_side_effects or terminal_released_with_side_effects
        )
        snapshot: dict[str, Any] = {
            "owner": {
                "agent_id": "agent-a",
                "root_run_id": "root-a",
                "delegation_targets": [] if checkpoint_missing_frozen_target else ["agent-b"],
                "max_tokens_per_run": 100,
                "max_cost_usd_per_run": None,
                "cost_enabled": False,
            },
            "registry_version": "registry-v1",
            "config_version": "config-v1",
            "catalog_version": "catalog-v1",
            "agents": {
                agent_id: {
                    "agent_id": agent_id,
                    "descriptor_version": f"{agent_id}-v1",
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
                for agent_id in ("agent-a", "agent-b")
            },
        }
        allocation_identity = OperationIdentity.from_semantic_request(
            tenant_id="tenant-a",
            fingerprint_key=b"legacy-postgresql-key",
            fingerprint_key_version="legacy-key-v1",
            ownership_kind="allocation",
            run_id="child-a",
            agent_id="agent-b",
            delegation_claim_id="delegation-a",
            usage_kind="model",
            operation_slot="turn:1:model",
            semantic_request={"prompt_ref": "legacy-child-request"},
            tree_snapshot_id="snapshot:legacy-pg-delegation",
            agent_sub_snapshot_id="snapshot:legacy-pg-delegation:agent-b",
            provider="fake",
            model="fake-basic",
            price_source_ref="catalog:fake",
            price_source_version="v1",
            cache_key_digest=None,
            cost_enabled=False,
            trusted_token_bound=20,
            trusted_cost_bound=None,
        ).to_payload()
        target_routes = cast(
            list[object],
            cast(dict[str, Any], cast(dict[str, Any], snapshot["agents"])["agent-b"])["routes"],
        )
        top_identity = OperationIdentity.from_delegation_request(
            tenant_id="tenant-a",
            fingerprint_key=b"legacy-postgresql-delegation-key",
            fingerprint_key_version="legacy-key-v1",
            canonical_request_bytes=b"request-hash-a",
            parent_run_id="root-a",
            source_agent_id="agent-a",
            target_agent_id="agent-b",
            delegation_claim_id="delegation-a",
            operation_slot="key-a",
            tree_snapshot_id="snapshot:legacy-pg-delegation",
            target_sub_snapshot_id="snapshot:legacy-pg-delegation:agent-b",
            target_route_catalog_digest=f"budget-routes-v1:{canonical_hash(target_routes)}",
            cost_enabled=False,
            trusted_token_bound=10,
            trusted_cost_bound=None,
        ).to_payload()
        actual_tokens = 12 if actual_over else None
        claim_state = "needs_review" if actual_over else "released"
        claim_impact = 12 if actual_over else 0
        bundle: dict[str, Any] = {
            "ledger": {
                "token_limit": 100,
                "cost_limit": None,
                "cost_enabled": False,
                "token_impact": claim_impact,
                "cost_impact": "0",
                "state": "needs_review" if actual_over else "active",
                "version": 1,
                "registry_version": "registry-v1",
                "config_version": "config-v1",
                "catalog_version": "catalog-v1",
                "snapshot_id": "snapshot:legacy-pg-delegation",
                "snapshot_hash": canonical_hash(snapshot),
                "snapshot": snapshot,
            },
            "claims": []
            if checkpoint_missing_reservation
            else [
                {
                    "id": "claim-delegation-a",
                    "operation_kind": "delegation",
                    "usage_call_id": None,
                    "delegation_id": "delegation-a",
                    "run_id": "root-a",
                    "agent_id": "agent-a",
                    "usage_kind": "delegation",
                    "identity_json": top_identity,
                    "request_hash": "request-hash-a",
                    "reserved_tokens": 10,
                    "reserved_cost": None,
                    "actual_tokens": actual_tokens,
                    "actual_cost": None,
                    "token_impact": claim_impact,
                    "cost_impact": "0",
                    "state": claim_state,
                    "side_effect_state": "result_committed" if actual_over else "not_started",
                    "result_json": (
                        {"outcome": "completed", "delegation_id": "delegation-a"}
                        if actual_over
                        else None
                    ),
                    "backfill_source": "legacy_settled" if actual_over else "legacy_released",
                }
            ],
            "allocations": (
                [
                    {
                        "id": "allocation-a",
                        "delegation_id": "delegation-a",
                        "usage_call_id": "usage-child-a",
                        "run_id": "child-a",
                        "agent_id": "agent-b",
                        "usage_kind": "model",
                        "identity_json": allocation_identity,
                        "reserved_tokens": None,
                        "reserved_cost": None,
                        "actual_tokens": 12,
                        "actual_cost": None,
                        "token_impact": 12,
                        "cost_impact": "0",
                        "state": "settled",
                        "side_effect_state": "result_committed",
                        "result_json": {
                            "outcome": "completed",
                            "evidence": {"provider_called": True},
                        },
                        "backfill_source": "legacy_settled",
                    }
                ]
                if actual_over
                else []
            ),
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
                    "input_json) values ('root-a','tenant-a','session-a','agent-a',:status,"
                    "'trace-a',cast('{}' as jsonb))"
                ),
                {"status": ("completed" if terminal_case else "running")},
            )
            if actual_over or any_released_side_effects:
                await connection.execute(
                    text(
                        "insert into agent_runs(id,tenant_id,session_id,agent_id,status,trace_id,"
                        "input_json,parent_run_id,idempotency_key,queue_operation_id,"
                        "queue_enqueue_state,queue_message_id) values ("
                        "'child-a','tenant-a','session-a','agent-b','completed','trace-a',"
                        "cast('{}' as jsonb),'root-a','delegation:delegation-a',"
                        ":queue_operation_id,:queue_state,:queue_message_id)"
                    ),
                    {
                        "queue_operation_id": (
                            "delegation:delegation-a" if any_released_side_effects else None
                        ),
                        "queue_state": "queued" if any_released_side_effects else None,
                        "queue_message_id": (
                            "delegation-message-a" if any_released_side_effects else None
                        ),
                    },
                )
            await connection.execute(
                text(
                    "insert into run_event_capacity(run_id,tenant_id,highest_persisted_seq,"
                    "outstanding_reserved_event_count,terminal_reservation) "
                    "values ('root-a','tenant-a',3,0,:terminal_reservation)"
                ),
                {"terminal_reservation": (0 if terminal_case else 1)},
            )
            if terminal_released_with_side_effects:
                await connection.execute(
                    text(
                        "insert into run_event_capacity(run_id,tenant_id,highest_persisted_seq,"
                        "outstanding_reserved_event_count,terminal_reservation) "
                        "values ('child-a','tenant-a',1,0,0)"
                    )
                )
                for event_id, run_id in (
                    ("terminal-root-a", "root-a"),
                    ("terminal-child-a", "child-a"),
                ):
                    await connection.execute(
                        text(
                            "insert into canonical_events("
                            "id,tenant_id,run_id,stream_id,event_type,seq,terminal,visibility,"
                            "trace_id,record_scope,envelope_json) values ("
                            ":event_id,'tenant-a',:run_id,:stream_id,'run.completed',1,true,"
                            "'public','trace-a','run',cast('{}' as jsonb))"
                        ),
                        {
                            "event_id": event_id,
                            "run_id": run_id,
                            "stream_id": f"stream-{run_id}",
                        },
                    )
            elif terminal_case:
                await connection.execute(
                    text(
                        "insert into canonical_events("
                        "id,tenant_id,run_id,stream_id,event_type,seq,terminal,visibility,"
                        "trace_id,record_scope,envelope_json) values ("
                        "'terminal-root-a','tenant-a','root-a','stream-root-a','run.completed',"
                        "1,true,'public','trace-a','run',cast('{}' as jsonb))"
                    )
                )
            await connection.execute(
                text(
                    "insert into agent_delegations("
                    "id,tenant_id,parent_run_id,child_run_id,source_agent_id,target_agent_id,"
                    "idempotency_key,request_hash,budget_intent,child_input_json,identity_json,"
                    "trace_id,status,error_json,event_operation_kind,event_registry_version,"
                    "reserved_event_count) values ("
                    "'delegation-a','tenant-a','root-a',:child,'agent-a','agent-b','key-a',"
                    "'request-hash-a','inherit_parent',cast('{}' as jsonb),cast('{}' as jsonb),"
                    "'trace-a',:status,cast(:error as jsonb),'delegation','v1',3)"
                ),
                {
                    "child": ("child-a" if actual_over or any_released_side_effects else None),
                    "status": (
                        "completed"
                        if actual_over
                        else "claimed"
                        if missing_reservation
                        else "failed"
                    ),
                    "error": json.dumps(
                        None
                        if actual_over or missing_reservation
                        else {"code": "delegation.execution_failed"}
                    ),
                },
            )
            if not missing_reservation:
                await connection.execute(
                    text(
                        "insert into delegation_budget_reservations("
                        "id,delegation_id,tenant_id,parent_run_id,reserved_tokens,"
                        "reserved_cost_usd,settled_input_tokens,settled_output_tokens,"
                        "settled_cost_usd,state) values ("
                        "'reservation-a','delegation-a','tenant-a','root-a',10,null,:input,"
                        ":output,:cost,:state)"
                    ),
                    {
                        "input": 12 if actual_over else None,
                        "output": 0 if actual_over else None,
                        "cost": 0 if actual_over else None,
                        "state": "settled" if actual_over else "released",
                    },
                )
            if actual_over or any_released_side_effects:
                child_result = {
                    "outcome": "completed",
                    "evidence": {"provider_called": True},
                }
                await connection.execute(
                    text(
                        "insert into delegation_aggregates("
                        "id,delegation_id,tenant_id,parent_run_id,child_run_id,status,summary_json,"
                        "evidence_refs_json) values ("
                        "'aggregate-a','delegation-a','tenant-a','root-a','child-a','complete',"
                        "cast(:summary as jsonb),cast(:refs as jsonb))"
                    ),
                    {
                        "summary": json.dumps(
                            {
                                "parent_run_id": "root-a",
                                "input_tokens": 12,
                                "output_tokens": 0,
                                "cost_usd": None,
                                "budget_status": "exceeded" if actual_over else "within_limit",
                            }
                        ),
                        "refs": json.dumps(["usage-child-a"]),
                    },
                )
                await connection.execute(
                    text(
                        "insert into run_evidence_outbox("
                        "id,tenant_id,run_id,usage_call_id,event_id,operation_kind,state,"
                        "result_json,reserved_event_count) values ("
                        "'outbox-child-a','tenant-a','child-a','usage-child-a','usage-event-a',"
                        "'model_usage','published',cast(:result as jsonb),2)"
                    ),
                    {"result": json.dumps(child_result)},
                )
            if mode in {
                "released-valid",
                "released-with-side-effects",
                "released-terminal-with-side-effects",
                "released-terminal-canonical-child-event",
                "released-checkpoint-missing-frozen-target",
            }:
                claimed = {
                    "delegation_id": "delegation-a",
                    "parent_run_id": "root-a",
                    "child_run_id": None,
                    "source_agent_id": "agent-a",
                    "target_agent_id": "agent-b",
                    "status": "claimed",
                    "trace_id": "trace-a",
                }
                failed = {**claimed, "status": "failed"}
                for sequence, phase, state, payload in (
                    (1, "claimed", "published", claimed),
                    (2, "child", "cancelled", claimed),
                    (3, "final", "published", failed),
                ):
                    await connection.execute(
                        text(
                            "insert into run_evidence_outbox("
                            "id,tenant_id,run_id,event_id,operation_kind,state,result_json,"
                            "reserved_event_count,group_id,sequence_in_group) values ("
                            ":id,'tenant-a','root-a',:event_id,'delegation',:state,"
                            "cast(:result as jsonb),1,'delegation:delegation-a:evidence',:sequence)"
                        ),
                        {
                            "id": f"outbox-{phase}",
                            "event_id": f"delegation:delegation-a:{phase}",
                            "state": state,
                            "result": json.dumps(payload),
                            "sequence": sequence,
                        },
                    )
            if terminal_canonical_child_event:
                payload = {
                    "delegation_id": "delegation-a",
                    "child_run_id": "ghost-child",
                }
                await connection.execute(
                    text(
                        "insert into canonical_events("
                        "id,tenant_id,run_id,stream_id,event_type,seq,terminal,visibility,"
                        "trace_id,record_scope,payload_json,envelope_json) values ("
                        "'delegation:delegation-a:child','tenant-a','root-a',"
                        "'delegation-child-stream','delegation.child.created',1,false,"
                        "'internal','trace-a','run',cast(:payload as jsonb),"
                        "cast(:envelope as jsonb))"
                    ),
                    {
                        "payload": json.dumps(payload),
                        "envelope": json.dumps(
                            {
                                "event_id": "delegation:delegation-a:child",
                                "tenant_id": "tenant-a",
                                "run_id": "root-a",
                                "event_type": "delegation.child.created",
                                "seq": 1,
                                "trace_id": "trace-a",
                                "record_scope": "run",
                                "payload": payload,
                            }
                        ),
                    },
                )
            if not terminal_case:
                await seed_postgresql_backfill_records(
                    connection,
                    tenant_id="tenant-a",
                    run_id="root-a",
                    bundle=bundle,
                    delegation_fingerprint_proofs=(
                        {}
                        if checkpoint_missing_reservation
                        else delegation_fingerprint_proofs(
                            top_identity,
                            delegation_id="delegation-a",
                            request_hash="request-hash-a",
                        )
                    ),
                    prefix="checkpoint-a",
                )
        await engine.dispose()

        if not accepted:
            expected_error = (
                "owner limits conflict with snapshot"
                if checkpoint_missing_frozen_target
                else "backfill delegation reservation is missing or ambiguous"
                if checkpoint_missing_reservation
                else "pending delegation evidence"
                if terminal_missing_reservation
                else "pending queue recovery"
                if terminal_released_with_side_effects
                else "released delegation proof is invalid"
                if terminal_case
                else "delegation linkage is invalid"
            )
            with pytest.raises(RuntimeError, match=expected_error):
                await asyncio.to_thread(run_migrations, dsn)
            assert await asyncio.to_thread(get_current_revision, dsn) == "0015_agent_delegation"
            return

        await asyncio.to_thread(run_migrations, dsn)
        engine = create_async_engine(dsn)
        async with engine.connect() as connection:
            claim = (
                await connection.execute(
                    text(
                        "select state,token_impact,backfill_source "
                        "from budget_operation_claims where delegation_id='delegation-a'"
                    )
                )
            ).one()
            ledger = (
                await connection.execute(
                    text("select state,token_impact from parent_budget_ledgers")
                )
            ).one()
        await engine.dispose()
        if actual_over:
            assert tuple(claim) == ("needs_review", 12, "legacy_settled")
            assert tuple(ledger) == ("needs_review", 12)
        else:
            assert tuple(claim) == ("released", 0, "legacy_released")
            assert tuple(ledger) == ("active", 0)
