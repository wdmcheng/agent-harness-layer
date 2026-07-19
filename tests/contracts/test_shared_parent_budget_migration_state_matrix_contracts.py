"""0016 delegation 四态迁移矩阵合同。"""

# 场景文件共享同一 SQLite migration helper 与 canonical hash 规则。
# ruff: noqa: F403, F405
from tests.contracts.test_shared_parent_budget_migration_contracts import *


@pytest.mark.parametrize(
    (
        "legacy_state",
        "claim_state",
        "settled_tokens",
        "claim_impact",
        "release_case",
        "accepted",
    ),
    [
        ("reserved", "reserved", None, 10, "none", True),
        ("released", "released", None, 0, "valid", True),
        ("released", "released", None, 0, "pending", True),
        ("needs_review", "needs_review", None, 10, "none", True),
        ("released", "released", None, 1, "valid", False),
        ("released", "released", None, 0, "missing-lifecycle", False),
        ("released", "released", None, 0, "unstable-payload", False),
        ("released", "released", None, 0, "child-side-effect", False),
        ("released", "released", None, 0, "terminal-child-side-effect", False),
        ("released", "released", None, 0, "terminal-canonical-child-event", False),
        ("released", "released", None, 0, "terminal-missing-reservation", False),
        ("released", "released", None, 0, "checkpoint-missing-reservation", False),
    ],
    ids=[
        "reserved",
        "released",
        "released-pending-recovery",
        "needs-review",
        "released-nonzero",
        "released-missing-lifecycle",
        "released-unstable-payload",
        "released-child-side-effect",
        "released-terminal-child-side-effect",
        "released-terminal-canonical-child-event",
        "released-terminal-missing-reservation",
        "released-checkpoint-missing-reservation",
    ],
)
def test_0016_backfills_0015_delegation_four_state_matrix(
    tmp_path: Path,
    legacy_state: str,
    claim_state: str,
    settled_tokens: int | None,
    claim_impact: int,
    release_case: str,
    accepted: bool,
) -> None:
    """0015 四态逐字段映射，含 settled actual-over 与 released 安全证明。"""

    path = tmp_path / f"delegation-four-state-{legacy_state}-{claim_impact}.sqlite3"
    run_migrations(sqlite_dsn(path), "0015_agent_delegation")
    snapshot: dict[str, Any] = {
        "owner": {
            "agent_id": "agent-a",
            "root_run_id": "root-a",
            "delegation_targets": ["agent-b"],
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
    result = {"outcome": "completed", "delegation_id": "delegation-a"}
    settled = settled_tokens is not None
    target_routes = cast(dict[str, Any], cast(dict[str, Any], snapshot["agents"])["agent-b"])[
        "routes"
    ]
    top_identity = OperationIdentity.from_delegation_request(
        tenant_id="tenant-a",
        fingerprint_key=b"legacy-test-key",
        fingerprint_key_version="legacy-key-v1",
        canonical_request_bytes=b"legacy-delegation-request-a",
        parent_run_id="root-a",
        source_agent_id="agent-a",
        target_agent_id="agent-b",
        delegation_claim_id="delegation-a",
        operation_slot="key-a",
        tree_snapshot_id="snapshot:legacy-four-state",
        target_sub_snapshot_id="snapshot:legacy-four-state:agent-b",
        target_route_catalog_digest=f"budget-routes-v1:{canonical_hash(target_routes)}",
        cost_enabled=False,
        trusted_token_bound=10,
        trusted_cost_bound=None,
    ).to_payload()
    claim = {
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
        "actual_tokens": settled_tokens,
        "actual_cost": None,
        "token_impact": claim_impact,
        "cost_impact": "0",
        "state": claim_state,
        "side_effect_state": "result_committed" if settled else "not_started",
        "result_json": result if settled else None,
        "backfill_source": f"legacy_{legacy_state}",
    }
    bundle = {
        "ledger": {
            "token_limit": 100,
            "cost_limit": None,
            "cost_enabled": False,
            "token_impact": claim_impact,
            "cost_impact": "0",
            "state": "needs_review" if claim_state == "needs_review" else "active",
            "version": 1,
            "registry_version": "registry-v1",
            "config_version": "config-v1",
            "catalog_version": "catalog-v1",
            "snapshot_id": "snapshot:legacy-four-state",
            "snapshot_hash": canonical_hash(snapshot),
            "snapshot": snapshot,
        },
        "claims": [] if release_case == "checkpoint-missing-reservation" else [claim],
        "allocations": [],
    }
    with sqlite3.connect(path) as connection:
        seed_identity(connection, "tenant-a")
        connection.execute(
            "insert into run_trace_bindings(trace_id,tenant_id,root_run_id) "
            "values ('trace-a','tenant-a','root-a')"
        )
        terminal_case = release_case.startswith("terminal-")
        missing_reservation = release_case in {
            "terminal-missing-reservation",
            "checkpoint-missing-reservation",
        }
        connection.execute(
            "insert into agent_runs(id,tenant_id,session_id,agent_id,status,trace_id,input_json) "
            "values ('root-a','tenant-a','session-tenant-a','agent-a',?,'trace-a','{}')",
            ("completed" if terminal_case else "running",),
        )
        connection.execute(
            "insert into run_event_capacity(run_id,tenant_id,highest_persisted_seq,"
            "outstanding_reserved_event_count,terminal_reservation) "
            "values ('root-a','tenant-a',0,0,?)",
            (0 if terminal_case else 1,),
        )
        relation_status = (
            "claimed"
            if missing_reservation
            else "completed"
            if settled
            else "failed"
            if legacy_state == "released"
            else "needs_review"
            if legacy_state == "needs_review"
            else "claimed"
        )
        connection.execute(
            "insert into agent_delegations(id,tenant_id,parent_run_id,source_agent_id,"
            "target_agent_id,idempotency_key,request_hash,budget_intent,child_input_json,"
            "identity_json,trace_id,status,error_json,event_operation_kind,event_registry_version,"
            "reserved_event_count) values ("
            "'delegation-a','tenant-a','root-a','agent-a','agent-b','key-a',"
            "'request-hash-a','inherit_parent','{}','{}','trace-a',?,?,'delegation','v1',3)",
            (
                relation_status,
                json.dumps({"code": "delegation.execution_failed"})
                if legacy_state == "released" and not missing_reservation
                else None,
            ),
        )
        if not missing_reservation:
            connection.execute(
                "insert into delegation_budget_reservations("
                "id,delegation_id,tenant_id,parent_run_id,reserved_tokens,reserved_cost_usd,"
                "settled_input_tokens,settled_output_tokens,settled_cost_usd,state) values ("
                "'reservation-a','delegation-a','tenant-a','root-a',10,null,?,?,?,?)",
                (
                    settled_tokens if settled else None,
                    0 if settled else None,
                    0 if settled else None,
                    legacy_state,
                ),
            )
        if (
            legacy_state == "released"
            and release_case != "missing-lifecycle"
            and not missing_reservation
        ):
            claimed_result = {
                "delegation_id": "delegation-a",
                "parent_run_id": "root-a",
                "child_run_id": None,
                "source_agent_id": "agent-a",
                "target_agent_id": "agent-b",
                "status": "claimed",
                "trace_id": "trace-a",
            }
            failed_result = {**claimed_result, "status": "failed"}
            if release_case == "unstable-payload":
                failed_result["trace_id"] = "wrong-trace"
            claimed_state = "result_persisted" if release_case == "pending" else "published"
            final_state = "result_persisted" if release_case == "pending" else "published"
            for sequence, phase, state, payload in (
                (1, "claimed", claimed_state, claimed_result),
                (
                    2,
                    "child",
                    "published" if release_case == "child-side-effect" else "cancelled",
                    claimed_result,
                ),
                (3, "final", final_state, failed_result),
            ):
                connection.execute(
                    "insert into run_evidence_outbox("
                    "id,tenant_id,run_id,event_id,operation_kind,state,result_json,"
                    "reserved_event_count,group_id,sequence_in_group) values (?,?,?,?,?,?,?,?,?,?)",
                    (
                        f"outbox-{phase}",
                        "tenant-a",
                        "root-a",
                        f"delegation:delegation-a:{phase}",
                        "delegation",
                        state,
                        json.dumps(payload),
                        1,
                        "delegation:delegation-a:evidence",
                        sequence,
                    ),
                )
            if release_case == "pending":
                connection.execute(
                    "update run_event_capacity set outstanding_reserved_event_count=2 "
                    "where run_id='root-a' and tenant_id='tenant-a'"
                )
        if release_case in {"child-side-effect", "terminal-child-side-effect"}:
            connection.execute(
                "insert into agent_runs("
                "id,tenant_id,session_id,agent_id,status,trace_id,parent_run_id,"
                "idempotency_key,input_json,queue_operation_id,queue_enqueue_state) values ("
                "'child-a','tenant-a','session-tenant-a','agent-b','failed','trace-a','root-a',"
                "'delegation:delegation-a','{}','queue-child-a','queued')"
            )
            connection.execute(
                "update agent_delegations set child_run_id='child-a' where id='delegation-a'"
            )
            if terminal_case:
                connection.execute(
                    "insert into run_event_capacity(run_id,tenant_id,highest_persisted_seq,"
                    "outstanding_reserved_event_count,terminal_reservation) "
                    "values ('child-a','tenant-a',0,0,0)"
                )
            provider_result = {
                "outcome": "completed",
                "evidence": {"provider_called": True},
            }
            connection.execute(
                "insert into run_evidence_outbox("
                "id,tenant_id,run_id,usage_call_id,event_id,operation_kind,state,result_json,"
                "reserved_event_count) values ("
                "'outbox-child-usage','tenant-a','child-a','usage-child-a','usage-child-event',"
                "'model_usage','published',?,2)",
                (json.dumps(provider_result),),
            )
        if release_case == "terminal-canonical-child-event":
            connection.execute(
                "insert into canonical_events("
                "id,tenant_id,run_id,stream_id,event_type,seq,terminal,visibility,trace_id,"
                "record_scope,payload_json,envelope_json) values ("
                "'delegation:delegation-a:child','tenant-a','root-a','delegation-child-stream',"
                "'delegation.child.created',1,0,'internal','trace-a','run',?,?)",
                (
                    json.dumps({"delegation_id": "delegation-a", "child_run_id": "ghost-child"}),
                    json.dumps(
                        {
                            "event_id": "delegation:delegation-a:child",
                            "tenant_id": "tenant-a",
                            "run_id": "root-a",
                            "event_type": "delegation.child.created",
                            "seq": 1,
                            "trace_id": "trace-a",
                            "record_scope": "run",
                            "payload": {
                                "delegation_id": "delegation-a",
                                "child_run_id": "ghost-child",
                            },
                        }
                    ),
                ),
            )
        if not terminal_case:
            seed_backfill_records(
                connection,
                tenant_id="tenant-a",
                run_id="root-a",
                bundle=bundle,
                delegation_fingerprint_proofs=(
                    {}
                    if release_case == "checkpoint-missing-reservation"
                    else delegation_fingerprint_proofs(
                        top_identity,
                        delegation_id="delegation-a",
                        request_hash="request-hash-a",
                    )
                ),
                prefix="checkpoint-a",
            )
        connection.commit()

    if not accepted:
        expected_error = (
            "backfill delegation reservation is missing or ambiguous"
            if release_case == "checkpoint-missing-reservation"
            else "pending queue recovery"
            if release_case == "terminal-child-side-effect"
            else "pending delegation evidence"
            if missing_reservation
            else "released delegation proof is invalid"
            if terminal_case
            else "delegation linkage is invalid"
        )
        with pytest.raises(RuntimeError, match=expected_error):
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
            "select state,token_impact,backfill_source from budget_operation_claims"
        ).fetchone() == (claim_state, claim_impact, f"legacy_{legacy_state}")
