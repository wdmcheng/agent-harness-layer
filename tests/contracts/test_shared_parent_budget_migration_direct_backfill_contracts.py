"""0016 direct identity 与 snapshot backfill 合同。"""

# 场景文件共享同一 SQLite migration helper 与 canonical hash 规则。
# ruff: noqa: F403, F405
from tests.contracts.test_shared_parent_budget_migration_contracts import *


@pytest.mark.parametrize(
    "identity_case",
    [
        "valid",
        "omit-claim",
        "hash-tamper",
        "usage-kind-mismatch",
        "cost-mode-mismatch",
        "trusted-token-mismatch",
        "unused-target-incomplete",
    ],
)
def test_0016_backfills_only_complete_checkpoint_snapshot_and_direct_identity(
    tmp_path: Path,
    identity_case: str,
) -> None:
    """Active tree 只接受 durable snapshot、identity 与 usage linkage 全部一致的 bundle。"""

    path = tmp_path / f"backfill-{identity_case}.sqlite3"
    run_migrations(sqlite_dsn(path), "0015_agent_delegation")
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
    if identity_case == "unused-target-incomplete":
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
    operation = OperationIdentity.from_semantic_request(
        tenant_id="tenant-a",
        fingerprint_key=b"legacy-test-key",
        fingerprint_key_version="legacy-key-v1",
        ownership_kind="direct",
        run_id="root-a",
        agent_id="agent-a",
        delegation_claim_id=None,
        usage_kind="embedding" if identity_case == "usage-kind-mismatch" else "model",
        operation_slot="turn:1:model",
        semantic_request={"prompt_ref": "legacy-request"},
        tree_snapshot_id="snapshot:legacy-a",
        agent_sub_snapshot_id="snapshot:legacy-a:agent-a",
        provider="fake",
        model="fake-basic",
        price_source_ref="catalog:fake",
        price_source_version="v1",
        cache_key_digest=(
            "legacy-cache-digest" if identity_case == "usage-kind-mismatch" else None
        ),
        cost_enabled=identity_case == "cost-mode-mismatch",
        trusted_token_bound=6 if identity_case == "trusted-token-mismatch" else 5,
        trusted_cost_bound=(Decimal("1") if identity_case == "cost-mode-mismatch" else None),
    ).to_payload()
    if identity_case == "hash-tamper":
        operation["model"] = "tampered-model"
    result = {"outcome": "completed", "evidence": {"provider_called": True}}
    bundle: dict[str, Any] = {
        "ledger": {
            "token_limit": 100,
            "cost_limit": None,
            "cost_enabled": False,
            "token_impact": 4,
            "cost_impact": "0",
            "state": "active",
            "version": 1,
            "registry_version": "registry-v1",
            "config_version": "config-v1",
            "catalog_version": "catalog-v1",
            "snapshot_id": "snapshot:legacy-a",
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
                "reserved_cost": None,
                "actual_tokens": 4,
                "actual_cost": None,
                "token_impact": 4,
                "cost_impact": "0",
                "state": "settled",
                "side_effect_state": "result_committed",
                "result_json": result,
                "backfill_source": "legacy_settled",
            }
        ],
        "allocations": [],
    }
    if identity_case == "omit-claim":
        bundle["claims"] = []
        bundle["ledger"]["token_impact"] = 0
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
            "insert into run_event_capacity(run_id,tenant_id,highest_persisted_seq,"
            "outstanding_reserved_event_count,terminal_reservation) "
            "values ('root-a','tenant-a',2,0,1)"
        )
        connection.execute(
            "insert into run_evidence_outbox("
            "id,tenant_id,run_id,usage_call_id,event_id,operation_kind,state,result_json,"
            "reserved_event_count) values ("
            "'outbox-a','tenant-a','root-a','usage-a','usage-event-a','model_usage',"
            "'published',?,2)",
            (json.dumps(result),),
        )
        connection.execute(
            "insert into checkpoints(id,tenant_id,run_id,sequence,resume_token,state_json) "
            "values ('checkpoint-a','tenant-a','root-a',1,'resume-a',?)",
            (json.dumps({"shared_budget_backfill_v1": bundle}),),
        )
        connection.commit()

    if identity_case != "valid":
        expected = (
            "omits or invents durable operation evidence"
            if identity_case == "omit-claim"
            else "target sub-snapshot is incomplete"
            if identity_case == "unused-target-incomplete"
            else "direct identity is invalid"
        )
        with pytest.raises(RuntimeError, match=expected):
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
            "select budget_owner_run_id,token_impact,state,snapshot_id from parent_budget_ledgers"
        ).fetchone() == ("root-a", 4, "active", "snapshot:legacy-a")
        assert connection.execute(
            "select operation_kind,usage_call_id,token_impact,state,backfill_source "
            "from budget_operation_claims"
        ).fetchone() == ("direct", "usage-a", 4, "settled", "legacy_settled")
