"""CLI-EVT-001 的 cursor、可见性、NDJSON、terminal 与只读合同。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
from tests.contracts.runtime_contract_helpers import sqlite_dsn
from tests.contracts.test_runtime_checkpoint_runs_contracts import ROOT, build_orchestrator
from typer.testing import CliRunner

import agent_harness.cli_events as cli_events
from agent_harness.cli import app
from agent_harness.events import (
    CanonicalEvent,
    CanonicalEventEnvelopeStateInvalid,
    CanonicalEventType,
    canonical_event_bytes,
)
from agent_harness.identity import IdentityContext


def _event(
    seq: int,
    *,
    run_id: str = "run-1",
    visibility: str = "public",
    terminal: bool = False,
) -> CanonicalEvent:
    event_type = CanonicalEventType.RUN_COMPLETED if terminal else CanonicalEventType.RUN_STARTED
    return CanonicalEvent(
        event_id=f"event-{seq}",
        tenant_id="default",
        run_id=run_id,
        event_type=event_type,
        seq=seq,
        visibility=visibility,
        terminal=terminal,
        trace_id="trace_11111111-1111-4111-8111-111111111111",
    )


@dataclass
class _Run:
    tenant_id: str
    trace_id: str = "trace_11111111-1111-4111-8111-111111111111"


class _Runs:
    def __init__(self, tenant_id: str = "default") -> None:
        self._run = _Run(tenant_id)

    async def get(self, run_id: str) -> _Run | None:
        return self._run if run_id == "run-1" else None


class _Uow:
    def __init__(self, tenant_id: str = "default") -> None:
        self.runs = _Runs(tenant_id)

    async def __aenter__(self) -> _Uow:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


class _Storage:
    def __init__(self, tenant_id: str = "default") -> None:
        self._tenant_id = tenant_id
        self.closed = False

    def uow(self) -> _Uow:
        return _Uow(self._tenant_id)

    async def dispose(self) -> None:
        self.closed = True


class _Policy:
    def __init__(self, *, deny: bool = False) -> None:
        self.deny = deny
        self.checks: list[str] = []

    async def require_allowed_readonly(self, check: Any) -> object:
        self.checks.append(check.action)
        if self.deny:
            from agent_harness.policy import PolicyDeniedError

            raise PolicyDeniedError("denied")
        return object()


class _Sink:
    def __init__(self, events: list[CanonicalEvent]) -> None:
        self.events = events
        self.read_after: list[int] = []

    async def contains_seq(self, *, run_id: str, seq: int, include_internal: bool) -> bool:
        return any(
            event.run_id == run_id
            and event.seq == seq
            and (include_internal or event.visibility == "public")
            for event in self.events
        )

    async def terminal_event(
        self,
        *,
        run_id: str,
        include_internal: bool,
    ) -> CanonicalEvent | None:
        visible = [
            event
            for event in self.events
            if event.run_id == run_id
            and event.terminal
            and (include_internal or event.visibility == "public")
        ]
        return visible[-1] if visible else None

    async def read_page(
        self,
        *,
        run_id: str,
        after_seq: int,
        include_internal: bool,
        max_events: int = 100,
        max_bytes: int = 1_048_576,
    ) -> list[CanonicalEvent]:
        del max_bytes
        self.read_after.append(after_seq)
        return [
            event
            for event in self.events
            if event.run_id == run_id
            and event.seq > after_seq
            and (include_internal or event.visibility == "public")
        ][:max_events]


class _OneEventPageSink(_Sink):
    async def read_page(
        self,
        *,
        run_id: str,
        after_seq: int,
        include_internal: bool,
        max_events: int = 100,
        max_bytes: int = 1_048_576,
    ) -> list[CanonicalEvent]:
        return await super().read_page(
            run_id=run_id,
            after_seq=after_seq,
            include_internal=include_internal,
            max_events=1,
            max_bytes=max_bytes,
        )


class _InvalidLegacySink(_Sink):
    async def read_page(self, **_kwargs: Any) -> list[CanonicalEvent]:
        raise CanonicalEventEnvelopeStateInvalid("persisted event is invalid")


def _runtime(events: list[CanonicalEvent], *, deny_internal: bool = False) -> Any:
    return cli_events.EventStreamRuntime(
        storage=cast(Any, _Storage()),
        sink=cast(Any, _Sink(events)),
        policy=cast(Any, _Policy(deny=deny_internal)),
        identity=IdentityContext.local_default(),
    )


@pytest.mark.asyncio
async def test_cli_stream_outputs_canonical_public_ndjson_and_stops_at_terminal() -> None:
    public = _event(1)
    internal = _event(2, visibility="internal")
    terminal = _event(3, terminal=True)
    runtime = _runtime([public, internal, terminal])
    lines: list[str] = []

    await cli_events.stream_event_lines(
        runtime,
        run_id="run-1",
        after_seq=0,
        include_internal=False,
        write_line=lines.append,
    )

    assert lines == [
        canonical_event_bytes(public).decode("utf-8"),
        canonical_event_bytes(terminal).decode("utf-8"),
    ]
    assert runtime.sink.read_after == [0]


@pytest.mark.asyncio
async def test_cli_stream_consumed_terminal_exits_without_read_or_output() -> None:
    runtime = _runtime([_event(1), _event(2, terminal=True)])
    lines: list[str] = []

    await cli_events.stream_event_lines(
        runtime,
        run_id="run-1",
        after_seq=2,
        include_internal=False,
        write_line=lines.append,
    )

    assert lines == []
    assert runtime.sink.read_after == []


@pytest.mark.asyncio
async def test_cli_stream_hidden_cursor_fails_without_read_or_stdout() -> None:
    runtime = _runtime([_event(1), _event(2, visibility="internal"), _event(3, terminal=True)])

    with pytest.raises(cli_events.CliEventStreamError) as raised:
        await cli_events.stream_event_lines(
            runtime,
            run_id="run-1",
            after_seq=2,
            include_internal=False,
            write_line=lambda _line: pytest.fail("invalid cursor must not write stdout"),
        )

    assert raised.value.code == "validation_error"
    assert runtime.sink.read_after == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "events",
    [
        [_event(1), _event(3, terminal=True)],
        [_event(1), _event(2, run_id="run-2"), _event(3, terminal=True)],
    ],
    ids=["run-gap", "other-run"],
)
async def test_cli_stream_gap_and_other_run_cursor_share_the_hidden_error(
    events: list[CanonicalEvent],
) -> None:
    runtime = _runtime(events)

    with pytest.raises(cli_events.CliEventStreamError) as raised:
        await cli_events.stream_event_lines(
            runtime,
            run_id="run-1",
            after_seq=2,
            include_internal=False,
            write_line=lambda _line: pytest.fail("invalid cursor must not write stdout"),
        )

    assert raised.value.code == "validation_error"
    assert str(raised.value) == "after-seq is not visible"
    assert runtime.sink.read_after == []


@pytest.mark.asyncio
async def test_cli_stream_slow_stdout_does_not_prefetch_second_page() -> None:
    runtime = _runtime([])
    runtime.sink = cast(Any, _OneEventPageSink([_event(1), _event(2, terminal=True)]))

    def interrupting_writer(_line: str) -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        await cli_events.stream_event_lines(
            runtime,
            run_id="run-1",
            after_seq=0,
            include_internal=False,
            write_line=interrupting_writer,
        )

    assert runtime.sink.read_after == [0]


@pytest.mark.asyncio
async def test_cli_stream_idle_interrupt_has_no_synthetic_output(monkeypatch: Any) -> None:
    runtime = _runtime([])
    lines: list[str] = []

    async def interrupt_sleep(_seconds: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_events.asyncio, "sleep", interrupt_sleep)
    with pytest.raises(KeyboardInterrupt):
        await cli_events.stream_event_lines(
            runtime,
            run_id="run-1",
            after_seq=0,
            include_internal=False,
            write_line=lines.append,
        )

    assert lines == []
    assert runtime.sink.read_after == [0]


@pytest.mark.parametrize("after_seq", ["-1", "2147483648", "not-a-seq"])
def test_cli_command_exposes_stable_cursor_error_and_closes_runtime(
    monkeypatch: Any,
    after_seq: str,
) -> None:
    runtime = _runtime([_event(1, terminal=True)])

    def build_runtime(**_kwargs: Any) -> Any:
        return runtime

    monkeypatch.setattr(cli_events, "_build_event_stream_runtime", build_runtime)

    result = CliRunner().invoke(
        app,
        ["events", "stream", "run-1", "--after-seq", after_seq],
    )

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "validation_error" in result.stderr
    # cursor 在 runtime 构造前失败，因此没有依赖需要关闭。
    assert runtime.storage.closed is False


def test_cli_command_legacy_invalid_row_is_stable_and_writes_no_stdout(
    monkeypatch: Any,
) -> None:
    runtime = _runtime([])
    runtime.sink = cast(Any, _InvalidLegacySink([]))

    def build_runtime(**_kwargs: Any) -> Any:
        return runtime

    monkeypatch.setattr(
        cli_events,
        "_build_event_stream_runtime",
        build_runtime,
    )

    result = CliRunner().invoke(app, ["events", "stream", "run-1"])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr.strip() == ("stream.event_state_invalid: persisted event is invalid")
    assert runtime.storage.closed is True


def test_cli_command_internal_policy_denial_has_empty_stdout(monkeypatch: Any) -> None:
    runtime = _runtime([_event(1, visibility="internal")], deny_internal=True)

    def build_runtime(**_kwargs: Any) -> Any:
        return runtime

    monkeypatch.setattr(cli_events, "_build_event_stream_runtime", build_runtime)

    result = CliRunner().invoke(app, ["events", "stream", "run-1", "--include-internal"])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "policy.denied" in result.stderr
    assert runtime.storage.closed is True


def test_cli_command_help_keeps_after_seq_cli_only() -> None:
    result = CliRunner().invoke(app, ["events", "stream", "--help"])

    assert result.exit_code == 0
    assert "--after-seq" in result.output
    assert "Last-Event-ID" not in result.output


def test_cli_command_reads_real_local_runtime_without_business_writes(tmp_path: Path) -> None:
    async def seed() -> tuple[str, Path]:
        orchestrator, storage, events_path = await build_orchestrator(tmp_path)
        try:
            created = await orchestrator.start_run(
                agent_id="fake-agent",
                input={"prompt": "cli-stream"},
                idempotency_key="cli-stream",
            )
            return created.run_id, events_path
        finally:
            await storage.dispose()

    run_id, events_path = asyncio.run(seed())
    db_path = tmp_path / "runtime.db"
    before_db = db_path.read_bytes()
    before_events = events_path.read_bytes()

    result = CliRunner().invoke(
        app,
        [
            "events",
            "stream",
            run_id,
            "--profile",
            "local",
            "--profiles-dir",
            str(ROOT / "templates" / "service-app" / "configs" / "profiles"),
            "--storage-dsn",
            sqlite_dsn(db_path),
            "--events-path",
            str(events_path),
        ],
    )

    assert result.exit_code == 0, result.stderr
    payloads = [line for line in result.stdout.splitlines() if line]
    assert [json.loads(line)["event_type"] for line in payloads] == [
        "run.started",
        "run.completed",
    ]
    assert db_path.read_bytes() == before_db
    assert events_path.read_bytes() == before_events
