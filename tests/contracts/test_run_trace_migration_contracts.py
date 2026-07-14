"""0013 canonical run trace 的 SQLite backfill 合同。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from tests.contracts.run_trace_migration_test_helpers import (
    prepare_0012a,
    seed_identity,
    seed_run,
    sqlite_dsn,
)

from agent_harness.storage import run_migrations


def test_0013_rebuilds_parseable_envelope_for_0011_canonical_event(
    tmp_path: Path,
) -> None:
    """0011 历史事件升级后仍保留完整字段，并可按 CanonicalEvent 读取。"""

    from agent_harness.events import CanonicalEvent

    path = tmp_path / "legacy-canonical-event.db"
    run_migrations(sqlite_dsn(path), "0011_eval_experiment_legacy_created_review")
    with sqlite3.connect(path) as connection:
        seed_identity(connection, "tenant-a")
        connection.execute(
            "insert into agent_runs("
            "id, tenant_id, session_id, agent_id, status, input_json"
            ") values ('legacy-run', 'tenant-a', 'session-tenant-a', 'agent-a', "
            "'created', '{}')"
        )
        connection.execute(
            "insert into canonical_events("
            "id, tenant_id, run_id, agent_id, event_type, seq, terminal, visibility, "
            "payload_json, payload_ref, request_id, trace_id, created_at"
            ") values ('legacy-event', 'tenant-a', 'legacy-run', 'agent-a', "
            "'run.started', 1, 0, 'public', ?, 'artifact://legacy-payload', "
            "'request-legacy', null, '2026-01-01 00:00:00+00:00')",
            (json.dumps({"input": "preserved"}),),
        )

    run_migrations(sqlite_dsn(path))

    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "select id, tenant_id, run_id, event_type, seq, envelope_json "
            "from canonical_events where id='legacy-event'"
        ).fetchone()
        assert row is not None
        assert connection.execute(
            "select count(*) from canonical_events where id='legacy-event'"
        ).fetchone() == (1,)

    event = CanonicalEvent.model_validate(json.loads(row[5]))
    assert (event.event_id, event.tenant_id, event.run_id) == tuple(row[:3])
    assert (event.event_type.value, event.seq) == tuple(row[3:5])
    assert event.agent_id == "agent-a"
    assert event.timestamp.isoformat() == "2026-01-01T00:00:00+00:00"
    assert event.payload == {"input": "preserved"}
    assert event.payload_ref == "artifact://legacy-payload"
    assert event.request_id == "request-legacy"
    assert event.trace_id is not None
    assert event.record_scope == "run"


def test_0013_backfills_complete_lineage_evidence_and_preserves_external_trace(
    tmp_path: Path,
) -> None:
    """单一 lineage 候选传播到 context/checkpoint/approval/event/tool/eval/audit/ref。"""

    path = tmp_path / "complete.db"
    prepare_0012a(path)
    with sqlite3.connect(path) as connection:
        seed_identity(connection, "tenant-a")
        seed_run(connection, "root-a", trace_id="Trace-A")
        seed_run(connection, "child-a", parent_run_id="root-a")
        connection.execute(
            "insert into checkpoints(id, tenant_id, run_id, sequence, resume_token, state_json) "
            "values ('checkpoint-a', 'tenant-a', 'child-a', 1, 'resume-a', '{}')"
        )
        connection.execute(
            """
            insert into approvals(
                id, tenant_id, run_id, agent_id, action, resource, reason, status,
                metadata_json, trace_id
            ) values ('approval-a', 'tenant-a', 'child-a', 'agent-a', 'write',
                'file:a', 'review', 'waiting', '{}', null)
            """
        )
        connection.execute(
            """
            insert into canonical_events(
                id, tenant_id, run_id, event_type, seq, terminal, visibility,
                trace_id, envelope_json
            ) values ('event-a', 'tenant-a', 'child-a', 'run.started', 1, 0,
                'public', null, '{"event_id":"event-a"}')
            """
        )
        connection.execute(
            """
            insert into tool_invocations(
                id, tenant_id, agent_id, run_id, tool_name, args_ref, status,
                trace_id, metadata_json
            ) values ('tool-a', 'tenant-a', 'agent-a', 'child-a', 'write',
                'artifact://args', 'completed', null, '{}')
            """
        )
        connection.execute(
            """
            insert into eval_cases(
                id, tenant_id, name, status, payload_json, agent_id, run_id,
                trace_id, dataset, source_refs_json, artifact_refs_json, metadata_json
            ) values ('case-a', 'tenant-a', 'case', 'approved', '{}', 'agent-a',
                'child-a', null, 'default', '[]', '[]', '{}')
            """
        )
        connection.execute(
            """
            insert into eval_runs(
                id, tenant_id, eval_case_id, run_id, status, agent_id, dataset,
                case_count, score_summary_json, provider_status_json
            ) values ('eval-a', 'tenant-a', 'case-a', 'child-a', 'completed',
                'agent-a', 'default', 1, '{}', '[]')
            """
        )
        connection.execute(
            """
            insert into eval_scores(
                id, tenant_id, eval_run_id, case_id, agent_id, run_id, trace_id,
                metric, value, metadata_json, provider_status_json
            ) values ('score-a', 'tenant-a', 'eval-a', 'case-a', 'agent-a',
                'child-a', null, 'quality', 1.0, '{}', '[]')
            """
        )
        connection.execute(
            """
            insert into trace_refs(
                id, tenant_id, run_id, provider, external_trace_id
            ) values ('ref-a', 'tenant-a', 'child-a', 'provider-a', 'external-keep')
            """
        )
        connection.execute(
            "insert into trace_refs(id, tenant_id, run_id, provider, external_trace_id) "
            "values ('ref-non-run', 'tenant-a', null, 'provider-a', 'external-independent')"
        )
        connection.execute(
            "insert into eval_runs(id, tenant_id, run_id, status, agent_id, dataset) "
            "values ('eval-aggregate', 'tenant-a', null, 'completed', 'agent-a', 'aggregate')"
        )
        connection.execute(
            "insert into audit_logs(id, tenant_id, action, payload_json) "
            "values ('audit-a', 'tenant-a', 'run.action', ?)",
            (json.dumps({"run_id": "child-a"}),),
        )

    run_migrations(sqlite_dsn(path))

    with sqlite3.connect(path) as connection:
        runs = connection.execute(
            "select id, trace_id, execution_context_json from agent_runs order by id"
        ).fetchall()
        binding = connection.execute(
            "select trace_id, tenant_id, root_run_id from run_trace_bindings"
        ).fetchone()
        checkpoint = connection.execute("select state_json from checkpoints").fetchone()
        approval = connection.execute("select trace_id from approvals").fetchone()
        event = connection.execute(
            "select trace_id, record_scope, envelope_json from canonical_events"
        ).fetchone()
        tool = connection.execute("select trace_id from tool_invocations").fetchone()
        case = connection.execute("select trace_id from eval_cases").fetchone()
        eval_run = connection.execute("select trace_id from eval_runs where id='eval-a'").fetchone()
        score = connection.execute("select trace_id from eval_scores").fetchone()
        trace_ref = connection.execute(
            "select trace_id, external_trace_id from trace_refs where id='ref-a'"
        ).fetchone()
        nullable_evidence = connection.execute(
            "select (select trace_id from eval_runs where id='eval-aggregate'), "
            "(select trace_id from trace_refs where id='ref-non-run')"
        ).fetchone()
        audit = connection.execute("select record_scope, payload_json from audit_logs").fetchone()

    assert {row[1] for row in runs} == {"Trace-A"}
    assert all(json.loads(row[2])["trace_id"] == "Trace-A" for row in runs)
    assert binding == ("Trace-A", "tenant-a", "root-a")
    assert json.loads(checkpoint[0])["trace_id"] == "Trace-A"
    assert approval == ("Trace-A",)
    assert event[:2] == ("Trace-A", "run")
    assert json.loads(event[2])["trace_id"] == "Trace-A"
    assert tool == case == eval_run == score == ("Trace-A",)
    assert trace_ref == ("Trace-A", "external-keep")
    assert nullable_evidence == (None, None)
    assert audit[0] == "run" and json.loads(audit[1])["trace_id"] == "Trace-A"


@pytest.mark.parametrize(
    ("envelope_run_id", "envelope_trace_id"),
    [("telemetry", None), ("legacy-trace", "legacy-trace")],
)
def test_0013_legacy_ordinary_telemetry_uses_nested_run_ownership(
    tmp_path: Path,
    envelope_run_id: str,
    envelope_trace_id: str | None,
) -> None:
    """ordinary telemetry 的合成 envelope run_id 不得伪造 AgentRun lineage。"""

    path = tmp_path / f"telemetry-{envelope_run_id}.db"
    prepare_0012a(path)
    envelope = {
        "event_id": f"event-{envelope_run_id}",
        "tenant_id": "tenant-a",
        "run_id": envelope_run_id,
        "event_type": "artifact.created",
        "seq": 1,
        "timestamp": "2026-01-01T00:00:00Z",
        "terminal": False,
        "visibility": "internal",
        "trace_id": envelope_trace_id,
        "payload": {
            "telemetry": {
                "name": "legacy.non-run",
                "record_type": "event",
                "context": {"tenant_id": "tenant-a", "run_id": None, "trace_id": None},
                "payload": {"safe": True},
                "payload_ref": None,
            }
        },
    }
    with sqlite3.connect(path) as connection:
        seed_identity(connection, "tenant-a")
        connection.execute("pragma foreign_keys=off")
        connection.execute(
            "insert into canonical_events("
            "id, tenant_id, run_id, event_type, seq, terminal, visibility, payload_json, "
            "trace_id, envelope_json) values (?, 'tenant-a', ?, 'artifact.created', 1, 0, "
            "'internal', ?, ?, ?)",
            (
                envelope["event_id"],
                envelope_run_id,
                json.dumps(envelope["payload"]),
                envelope_trace_id,
                json.dumps(envelope),
            ),
        )

    run_migrations(sqlite_dsn(path))

    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "select run_id, stream_id, trace_id, record_scope, envelope_json from canonical_events"
        ).fetchone()
    assert row[:4] == (None, envelope_run_id, envelope_trace_id, "non_run")
    migrated_envelope = json.loads(row[4])
    assert migrated_envelope["run_id"] == envelope_run_id
    assert migrated_envelope["trace_id"] == envelope_trace_id


def test_0013_legacy_ordinary_telemetry_nested_run_is_validated_and_backfilled(
    tmp_path: Path,
) -> None:
    """nested context.run_id 是 ordinary telemetry 唯一真实 run 归属。"""

    path = tmp_path / "telemetry-run.db"
    prepare_0012a(path)
    envelope = {
        "event_id": "event-telemetry-run",
        "tenant_id": "tenant-a",
        "run_id": "telemetry",
        "event_type": "artifact.created",
        "seq": 1,
        "timestamp": "2026-01-01T00:00:00Z",
        "terminal": False,
        "visibility": "internal",
        "trace_id": None,
        "payload": {
            "telemetry": {
                "name": "legacy.run",
                "record_type": "event",
                "context": {"tenant_id": "tenant-a", "run_id": "root-a", "trace_id": None},
                "payload": {},
                "payload_ref": None,
            }
        },
    }
    with sqlite3.connect(path) as connection:
        seed_identity(connection, "tenant-a")
        seed_run(connection, "root-a", trace_id="Trace-A")
        connection.execute("pragma foreign_keys=off")
        connection.execute(
            "insert into canonical_events("
            "id, tenant_id, run_id, event_type, seq, terminal, visibility, payload_json, "
            "trace_id, envelope_json) values ('event-telemetry-run', 'tenant-a', "
            "'telemetry', 'artifact.created', 1, 0, 'internal', ?, null, ?)",
            (json.dumps(envelope["payload"]), json.dumps(envelope)),
        )

    run_migrations(sqlite_dsn(path))

    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "select run_id, stream_id, trace_id, record_scope, envelope_json from canonical_events"
        ).fetchone()
    assert row[:4] == ("root-a", "telemetry", "Trace-A", "run")
    migrated_envelope = json.loads(row[4])
    assert migrated_envelope["run_id"] == "telemetry"
    assert migrated_envelope["trace_id"] == "Trace-A"
