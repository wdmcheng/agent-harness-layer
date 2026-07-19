"""真实 PostgreSQL EventSink reader 合同。"""

from __future__ import annotations

import json
import os
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import text
from tests.contracts.embedding_cache_postgresql_migration_contract_helpers import (
    isolated_database,
)

from agent_harness.cli_events import EventStreamRuntime, stream_event_lines
from agent_harness.events import (
    CanonicalEvent,
    CanonicalEventEnvelopeStateInvalid,
    CanonicalEventType,
    PostgreSQLEventSink,
    canonical_event_bytes,
)
from agent_harness.identity import IdentityContext
from agent_harness.storage import RunCreate, SessionCreate, SQLAlchemyStorage, run_migrations
from agent_harness.storage.evidence_repositories import MAX_EVENT_SEQ, EventSequenceStateInvalid

pytestmark = pytest.mark.skipif(
    not os.environ.get("AGENT_HARNESS_TEST_POSTGRES_DSN"),
    reason="真实 PostgreSQL SSE reader 合同需要 AGENT_HARNESS_TEST_POSTGRES_DSN。",
)


class _AllowReadonlyPolicy:
    """真实 PostgreSQL CLI adapter 合同只替换 policy 决策，不替换 reader。"""

    def __init__(self) -> None:
        self.actions: list[str] = []

    async def require_allowed_readonly(self, check: Any) -> object:
        self.actions.append(check.action)
        return object()


async def _seed_run(storage: SQLAlchemyStorage, suffix: str) -> tuple[str, str, str]:
    tenant_id = f"sse-reader-{suffix}-{uuid4()}"
    trace_id = f"trace-{suffix}-{uuid4()}"
    async with storage.uow() as uow:
        await uow.tenants.ensure(tenant_id)
        session = await uow.sessions.create(
            SessionCreate(tenant_id=tenant_id, user_id="user-a", agent_id="agent-a")
        )
        run = await uow.runs.create(
            RunCreate(
                tenant_id=tenant_id,
                session_id=session.id,
                agent_id="agent-a",
                trace_id=trace_id,
            )
        )
        await uow.commit()
    return tenant_id, run.id, trace_id


def _event(
    *,
    event_id: str,
    tenant_id: str,
    run_id: str,
    trace_id: str,
    visibility: str,
    terminal: bool = False,
) -> CanonicalEvent:
    return CanonicalEvent(
        event_id=event_id,
        tenant_id=tenant_id,
        run_id=run_id,
        agent_id="agent-a",
        event_type=(
            CanonicalEventType.RUN_COMPLETED if terminal else CanonicalEventType.RUN_STARTED
        ),
        seq=0,
        terminal=terminal,
        visibility=visibility,
        trace_id=trace_id,
    )


async def _seed_sparse_high_water(
    storage: SQLAlchemyStorage,
    *,
    tenant_id: str,
    run_id: str,
    trace_id: str,
) -> None:
    sparse = CanonicalEvent(
        event_id=f"sparse-{run_id}",
        tenant_id=tenant_id,
        run_id=run_id,
        agent_id="agent-a",
        event_type=CanonicalEventType.RUN_STARTED,
        seq=MAX_EVENT_SEQ - 1,
        visibility="public",
        trace_id=trace_id,
    )
    async with storage.engine.begin() as connection:
        await connection.execute(
            text(
                "insert into canonical_events("
                "id, tenant_id, run_id, stream_id, agent_id, event_type, seq, terminal, "
                "visibility, trace_id, record_scope, envelope_json) values ("
                ":event_id, :tenant_id, :run_id, :run_id, 'agent-a', 'run.started', "
                ":seq, false, 'public', :trace_id, 'run', cast(:envelope as json))"
            ),
            {
                "event_id": sparse.event_id,
                "tenant_id": tenant_id,
                "run_id": run_id,
                "seq": sparse.seq,
                "trace_id": trace_id,
                "envelope": json.dumps(sparse.to_payload(), ensure_ascii=False),
            },
        )
        await connection.execute(
            text("update run_event_capacity set highest_persisted_seq=:seq where run_id=:run_id"),
            {"seq": sparse.seq, "run_id": run_id},
        )


@pytest.mark.asyncio
async def test_postgresql_reader_visibility_membership_resume_and_terminal() -> None:
    async with isolated_database("sse_reader_page") as dsn:
        run_migrations(dsn)
        storage = SQLAlchemyStorage.from_dsn(dsn)
        try:
            tenant_id, run_id, trace_id = await _seed_run(storage, "page")
            sink = PostgreSQLEventSink(storage)
            public = await sink.write(
                _event(
                    event_id="public",
                    tenant_id=tenant_id,
                    run_id=run_id,
                    trace_id=trace_id,
                    visibility="public",
                )
            )
            internal = await sink.write(
                _event(
                    event_id="internal",
                    tenant_id=tenant_id,
                    run_id=run_id,
                    trace_id=trace_id,
                    visibility="internal",
                )
            )
            terminal = await sink.write(
                _event(
                    event_id="terminal",
                    tenant_id=tenant_id,
                    run_id=run_id,
                    trace_id=trace_id,
                    visibility="public",
                    terminal=True,
                )
            )

            assert [event.seq for event in await sink.read_page(run_id=run_id)] == [1, 3]
            assert [
                event.seq
                for event in await sink.read_page(
                    run_id=run_id,
                    after_seq=public.seq,
                    include_internal=True,
                )
            ] == [2, 3]
            assert await sink.contains_seq(run_id=run_id, seq=internal.seq) is False
            assert (
                await sink.contains_seq(
                    run_id=run_id,
                    seq=internal.seq,
                    include_internal=True,
                )
                is True
            )
            assert await sink.terminal_event(run_id=run_id) == terminal
        finally:
            await storage.dispose()


@pytest.mark.asyncio
async def test_cli_stream_uses_real_postgresql_reader_and_canonical_ndjson() -> None:
    async with isolated_database("sse_cli_reader") as dsn:
        run_migrations(dsn)
        storage = SQLAlchemyStorage.from_dsn(dsn)
        try:
            tenant_id, run_id, trace_id = await _seed_run(storage, "cli")
            sink = PostgreSQLEventSink(storage)
            public = await sink.write(
                _event(
                    event_id="cli-public",
                    tenant_id=tenant_id,
                    run_id=run_id,
                    trace_id=trace_id,
                    visibility="public",
                )
            )
            internal = await sink.write(
                _event(
                    event_id="cli-internal",
                    tenant_id=tenant_id,
                    run_id=run_id,
                    trace_id=trace_id,
                    visibility="internal",
                )
            )
            terminal = await sink.write(
                _event(
                    event_id="cli-terminal",
                    tenant_id=tenant_id,
                    run_id=run_id,
                    trace_id=trace_id,
                    visibility="public",
                    terminal=True,
                )
            )
            policy = _AllowReadonlyPolicy()
            runtime = EventStreamRuntime(
                storage=storage,
                sink=sink,
                policy=cast(Any, policy),
                identity=IdentityContext(
                    tenant_id=tenant_id,
                    user_id="user-a",
                    session_id="cli-postgresql-contract",
                ),
            )

            public_lines: list[str] = []
            await stream_event_lines(
                runtime,
                run_id=run_id,
                after_seq=0,
                include_internal=False,
                write_line=public_lines.append,
            )
            internal_lines: list[str] = []
            await stream_event_lines(
                runtime,
                run_id=run_id,
                after_seq=public.seq,
                include_internal=True,
                write_line=internal_lines.append,
            )

            assert public_lines == [
                canonical_event_bytes(public).decode("utf-8"),
                canonical_event_bytes(terminal).decode("utf-8"),
            ]
            assert internal_lines == [
                canonical_event_bytes(internal).decode("utf-8"),
                canonical_event_bytes(terminal).decode("utf-8"),
            ]
            assert policy.actions == ["events.read_internal"]
        finally:
            await storage.dispose()


@pytest.mark.asyncio
async def test_postgresql_reader_fails_closed_on_public_direct_write_oversized_row() -> None:
    async with isolated_database("sse_reader_oversized") as dsn:
        run_migrations(dsn)
        storage = SQLAlchemyStorage.from_dsn(dsn)
        try:
            tenant_id, run_id, trace_id = await _seed_run(storage, "oversized")
            oversized = CanonicalEvent(
                event_id="legacy-public-oversized",
                tenant_id=tenant_id,
                run_id=run_id,
                agent_id="agent-a",
                event_type=CanonicalEventType.RUN_STARTED,
                seq=1,
                payload={"legacy": "中" * 30_000},
                visibility="public",
                trace_id=trace_id,
            )
            async with storage.engine.begin() as connection:
                await connection.execute(
                    text(
                        "insert into canonical_events("
                        "id, tenant_id, run_id, stream_id, agent_id, event_type, seq, terminal, "
                        "visibility, payload_json, trace_id, record_scope, envelope_json) values ("
                        ":event_id, :tenant_id, :run_id, :run_id, 'agent-a', 'run.started', 1, "
                        "false, 'public', cast(:payload as json), :trace_id, 'run', "
                        "cast(:envelope as json))"
                    ),
                    {
                        "event_id": oversized.event_id,
                        "tenant_id": tenant_id,
                        "run_id": run_id,
                        "trace_id": trace_id,
                        "payload": json.dumps(oversized.payload, ensure_ascii=False),
                        "envelope": json.dumps(oversized.to_payload(), ensure_ascii=False),
                    },
                )

            with pytest.raises(CanonicalEventEnvelopeStateInvalid) as rejected:
                await PostgreSQLEventSink(storage).read_page(run_id=run_id)
            assert rejected.value.code == "event.envelope_state_invalid"
        finally:
            await storage.dispose()


@pytest.mark.asyncio
async def test_postgresql_sparse_seq_max_is_reserved_for_terminal_without_partial_write() -> None:
    async with isolated_database("sse_capacity_max") as dsn:
        run_migrations(dsn)
        storage = SQLAlchemyStorage.from_dsn(dsn)
        try:
            invalid_scope = await _seed_run(storage, "invalid-max")
            terminal_scope = await _seed_run(storage, "terminal-max")
            await _seed_sparse_high_water(
                storage,
                tenant_id=invalid_scope[0],
                run_id=invalid_scope[1],
                trace_id=invalid_scope[2],
            )
            await _seed_sparse_high_water(
                storage,
                tenant_id=terminal_scope[0],
                run_id=terminal_scope[1],
                trace_id=terminal_scope[2],
            )
            sink = PostgreSQLEventSink(storage)

            with pytest.raises(EventSequenceStateInvalid) as rejected:
                await sink.write(
                    _event(
                        event_id="invalid-max",
                        tenant_id=invalid_scope[0],
                        run_id=invalid_scope[1],
                        trace_id=invalid_scope[2],
                        visibility="public",
                    )
                )
            assert rejected.value.code == "event.sequence_state_invalid"
            assert [event.seq for event in await sink.read(run_id=invalid_scope[1])] == [
                MAX_EVENT_SEQ - 1
            ]

            terminal = await sink.write(
                _event(
                    event_id="terminal-max",
                    tenant_id=terminal_scope[0],
                    run_id=terminal_scope[1],
                    trace_id=terminal_scope[2],
                    visibility="public",
                    terminal=True,
                )
            )
            assert terminal.seq == MAX_EVENT_SEQ
            assert await sink.terminal_event(run_id=terminal_scope[1]) == terminal
        finally:
            await storage.dispose()
