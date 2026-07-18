"""0016 delegation claim 与 child allocation backfill 合同。"""

# 场景文件共享同一 SQLite migration helper 与 canonical hash 规则。
# ruff: noqa: F403, F405
from tests.contracts.test_shared_parent_budget_migration_contracts import *


@pytest.mark.parametrize("omit_frozen_target", [False, True])
@pytest.mark.parametrize(
    ("actual_tokens", "claim_state"),
    [(3, "settled"), (12, "needs_review")],
)
def test_0016_backfills_delegation_claim_and_child_allocation_from_one_tree_bundle(
    tmp_path: Path,
    actual_tokens: int,
    claim_state: str,
    omit_frozen_target: bool,
) -> None:
    """Child 复用 root snapshot；settled actual-over 保守提升为 needs_review。"""

    path = tmp_path / "backfill-allocation.sqlite3"
    run_migrations(sqlite_dsn(path), "0015_agent_delegation")
    snapshot: dict[str, Any] = {
        "owner": {
            "agent_id": "agent-a",
            "root_run_id": "root-a",
            "delegation_targets": [] if omit_frozen_target else ["agent-b"],
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
            },
            "agent-b": {
                "agent_id": "agent-b",
                "descriptor_version": "agent-b-v2",
                "model_policy": {
                    "provider": "fake",
                    "default_model": "fake-child",
                    "fallback_models": [],
                },
                "target_budget": {
                    "max_tokens_per_run": 20,
                    "max_cost_usd_per_run": None,
                },
                "routes": [
                    {
                        "usage_kind": "model",
                        "provider": "fake",
                        "model": "fake-child",
                        "price_source_ref": "catalog:fake-child",
                        "price_source_version": "v2",
                        "input_token_price_usd": "0",
                        "output_token_price_usd": "0",
                    }
                ],
            },
        },
    }
    identity = OperationIdentity.from_semantic_request(
        tenant_id="tenant-a",
        fingerprint_key=b"legacy-test-key",
        fingerprint_key_version="legacy-key-v1",
        ownership_kind="allocation",
        run_id="child-a",
        agent_id="agent-b",
        delegation_claim_id="delegation-a",
        usage_kind="model",
        operation_slot="turn:1:model",
        semantic_request={"prompt_ref": "legacy-child-request"},
        tree_snapshot_id="snapshot:legacy-tree",
        agent_sub_snapshot_id="snapshot:legacy-tree:agent-b",
        provider="fake",
        model="fake-child",
        price_source_ref="catalog:fake-child",
        price_source_version="v2",
        cache_key_digest=None,
        cost_enabled=False,
        trusted_token_bound=20,
        trusted_cost_bound=None,
    ).to_payload()
    child_result = {"outcome": "completed", "evidence": {"provider_called": True}}
    top_level_impact = actual_tokens if claim_state == "settled" else max(10, actual_tokens)
    bundle: dict[str, Any] = {
        "ledger": {
            "token_limit": 100,
            "cost_limit": None,
            "cost_enabled": False,
            "token_impact": top_level_impact,
            "cost_impact": "0",
            "state": "needs_review" if claim_state == "needs_review" else "active",
            "version": 2,
            "registry_version": "registry-v1",
            "config_version": "config-v1",
            "catalog_version": "catalog-v1",
            "snapshot_id": "snapshot:legacy-tree",
            "snapshot_hash": canonical_hash(snapshot),
            "snapshot": snapshot,
        },
        "claims": [
            {
                "id": "claim-delegation-a",
                "operation_kind": "delegation",
                "usage_call_id": None,
                "delegation_id": "delegation-a",
                "run_id": "root-a",
                "agent_id": "agent-a",
                "usage_kind": None,
                "identity_json": None,
                "request_hash": "request-hash-a",
                "reserved_tokens": 10,
                "reserved_cost": None,
                "actual_tokens": actual_tokens,
                "actual_cost": None,
                "token_impact": top_level_impact,
                "cost_impact": "0",
                "state": claim_state,
                "side_effect_state": "result_committed",
                "result_json": {
                    "outcome": "completed",
                    "delegation_id": "delegation-a",
                },
                "backfill_source": "legacy_settled",
            }
        ],
        "allocations": [
            {
                "id": "allocation-a",
                "delegation_id": "delegation-a",
                "usage_call_id": "usage-child-a",
                "run_id": "child-a",
                "agent_id": "agent-b",
                "usage_kind": "model",
                "identity_json": identity,
                "reserved_tokens": None,
                "reserved_cost": None,
                "actual_tokens": actual_tokens,
                "actual_cost": None,
                "token_impact": actual_tokens,
                "cost_impact": "0",
                "state": "settled",
                "side_effect_state": "result_committed",
                "result_json": child_result,
                "backfill_source": "legacy_settled",
            }
        ],
    }
    with sqlite3.connect(path) as connection:
        seed_identity(connection, "tenant-a")
        connection.execute(
            "insert into run_trace_bindings(trace_id,tenant_id,root_run_id) "
            "values ('trace-a','tenant-a','root-a')"
        )
        connection.execute(
            "insert into agent_runs(id,tenant_id,session_id,agent_id,status,trace_id,input_json) "
            "values ('root-a','tenant-a','session-tenant-a','agent-a','running','trace-a','{}')"
        )
        connection.execute(
            "insert into agent_runs(id,tenant_id,session_id,agent_id,status,trace_id,input_json,"
            "parent_run_id,idempotency_key) values ("
            "'child-a','tenant-a','session-tenant-a','agent-b','completed','trace-a','{}',"
            "'root-a','delegation:delegation-a')"
        )
        connection.execute(
            "insert into agent_delegations(id,tenant_id,parent_run_id,child_run_id,source_agent_id,"
            "target_agent_id,idempotency_key,request_hash,budget_intent,child_input_json,"
            "identity_json,trace_id,status,event_operation_kind,event_registry_version,"
            "reserved_event_count) values ("
            "'delegation-a','tenant-a','root-a','child-a','agent-a','agent-b','key-a',"
            "'request-hash-a','inherit_parent','{}','{}','trace-a','completed','delegation','v1',3)"
        )
        connection.execute(
            "insert into delegation_budget_reservations(id,delegation_id,tenant_id,parent_run_id,"
            "reserved_tokens,reserved_cost_usd,settled_input_tokens,settled_output_tokens,"
            "settled_cost_usd,state) values ("
            "'reservation-a','delegation-a','tenant-a','root-a',10,null,?,0,0,'settled')",
            (actual_tokens,),
        )
        connection.execute(
            "insert into delegation_aggregates("
            "id,delegation_id,tenant_id,parent_run_id,child_run_id,status,summary_json,"
            "evidence_refs_json) values ("
            "'aggregate-a','delegation-a','tenant-a','root-a','child-a','complete',?,?)",
            (
                json.dumps(
                    {
                        "parent_run_id": "root-a",
                        "input_tokens": actual_tokens,
                        "output_tokens": 0,
                        "cost_usd": None,
                        "budget_status": (
                            "exceeded" if claim_state == "needs_review" else "within_budget"
                        ),
                    }
                ),
                json.dumps(["usage-child-a"]),
            ),
        )
        connection.execute(
            "insert into run_evidence_outbox(id,tenant_id,run_id,usage_call_id,event_id,"
            "operation_kind,state,result_json,reserved_event_count) values ("
            "'outbox-child-a','tenant-a','child-a','usage-child-a','usage-event-child-a',"
            "'model_usage','published',?,2)",
            (json.dumps(child_result),),
        )
        connection.execute(
            "insert into checkpoints(id,tenant_id,run_id,sequence,resume_token,state_json) "
            "values ('checkpoint-a','tenant-a','root-a',1,'resume-a',?)",
            (json.dumps({"shared_budget_backfill_v1": bundle}),),
        )
        connection.commit()

    if omit_frozen_target:
        with pytest.raises(RuntimeError, match="owner limits conflict with snapshot"):
            run_migrations(sqlite_dsn(path))
        with sqlite3.connect(path) as connection:
            assert connection.execute(
                "select count(*) from sqlite_master "
                "where type='table' and name='parent_budget_ledgers'"
            ).fetchone() == (0,)
        return

    run_migrations(sqlite_dsn(path))
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "select operation_kind,delegation_id,state,token_impact from budget_operation_claims"
        ).fetchone() == (
            "delegation",
            "delegation-a",
            claim_state,
            top_level_impact,
        )
        assert connection.execute(
            "select budget_owner_run_id,delegation_id,run_id,agent_id,state,token_impact "
            "from delegation_budget_allocations"
        ).fetchone() == (
            "root-a",
            "delegation-a",
            "child-a",
            "agent-b",
            "settled",
            actual_tokens,
        )
