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
    """构造固定租户与 trace 的最小事件，供 cursor、可见性和终结语义断言复用。"""

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
    """只暴露事件流授权所需租户与 trace 的极简 run 替身。"""

    tenant_id: str
    trace_id: str = "trace_11111111-1111-4111-8111-111111111111"


class _Runs:
    """固定返回 ``run-1`` 的 run 仓储替身，用于隔离事件流读取逻辑。"""

    def __init__(self, tenant_id: str = "default") -> None:
        """创建指定租户的唯一可见 run。"""

        self._run = _Run(tenant_id)

    async def get(self, run_id: str) -> _Run | None:
        """仅让约定 run id 可见，其他 id 模拟不存在或不可见的授权边界。"""

        return self._run if run_id == "run-1" else None


class _Uow:
    """事件流读取所需的最小异步工作单元替身，不持有真实数据库事务。"""

    def __init__(self, tenant_id: str = "default") -> None:
        """装配只读 run 仓储，保持生产调用路径所需属性可用。"""

        self.runs = _Runs(tenant_id)

    async def __aenter__(self) -> _Uow:
        """返回自身，使测试替身兼容生产代码的异步上下文协议。"""

        return self

    async def __aexit__(self, *_args: object) -> None:
        """替身没有事务资源；保留空退出以验证调用方正确使用上下文。"""

        return None


class _Storage:
    """记录 dispose 调用的最小 storage 替身，验证 CLI 各错误路径的资源清理。"""

    def __init__(self, tenant_id: str = "default") -> None:
        """保存授权可见租户并初始化关闭标记。"""

        self._tenant_id = tenant_id
        self.closed = False

    def uow(self) -> _Uow:
        """返回新的只读工作单元替身，不创建真实数据库连接。"""

        return _Uow(self._tenant_id)

    async def dispose(self) -> None:
        """记录运行时释放动作，供命令行成功和失败路径断言。"""

        self.closed = True


class _Policy:
    """只控制 internal 读取是否被拒绝的策略替身，并记录调用动作。"""

    def __init__(self, *, deny: bool = False) -> None:
        """配置是否拒绝读取，并初始化检查动作记录。"""

        self.deny = deny
        self.checks: list[str] = []

    async def require_allowed_readonly(self, check: Any) -> object:
        """记录只读授权检查；配置拒绝时抛出与生产路径一致的策略异常。"""

        self.checks.append(check.action)
        if self.deny:
            from agent_harness.policy import PolicyDeniedError

            raise PolicyDeniedError("denied")
        return object()


class _Sink:
    """内存事件读取器替身，保留 cursor 读取历史以验证流式读取时机。"""

    def __init__(self, events: list[CanonicalEvent]) -> None:
        """保存按调用方提供顺序排列的事件和每次读取 cursor。"""

        self.events = events
        self.read_after: list[int] = []

    async def contains_seq(self, *, run_id: str, seq: int, include_internal: bool) -> bool:
        """按当前可见性视图判断 cursor 是否存在，模拟生产 reader 的校验语义。"""

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
        """返回当前可见性视图中最后一个终结事件，用于 consumed cursor 快速退出。"""

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
        """记录读取 cursor 后返回可见后续事件，忽略字节限制以聚焦 CLI 控制流。"""

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
    """每次只返回一条事件的读取器替身，用于验证慢 stdout 不会预取下一页。"""

    async def read_page(
        self,
        *,
        run_id: str,
        after_seq: int,
        include_internal: bool,
        max_events: int = 100,
        max_bytes: int = 1_048_576,
    ) -> list[CanonicalEvent]:
        """强制将分页大小限制为一条，保留父类的 cursor 记录与可见性逻辑。"""

        return await super().read_page(
            run_id=run_id,
            after_seq=after_seq,
            include_internal=include_internal,
            max_events=1,
            max_bytes=max_bytes,
        )


class _InvalidLegacySink(_Sink):
    """模拟历史持久化事件损坏的读取器，用于验证 CLI 错误映射和无 stdout 约束。"""

    async def read_page(self, **_kwargs: Any) -> list[CanonicalEvent]:
        """在实际读取入口抛出 envelope 状态错误，模拟旧数据解析失败。"""

        raise CanonicalEventEnvelopeStateInvalid("persisted event is invalid")


def _runtime(events: list[CanonicalEvent], *, deny_internal: bool = False) -> Any:
    """使用内存 storage、sink 和 policy 构造事件流运行时，避免测试依赖真实服务。"""

    return cli_events.EventStreamRuntime(
        storage=cast(Any, _Storage()),
        sink=cast(Any, _Sink(events)),
        policy=cast(Any, _Policy(deny=deny_internal)),
        identity=IdentityContext.local_default(),
    )


@pytest.mark.asyncio
async def test_cli_stream_outputs_canonical_public_ndjson_and_stops_at_terminal() -> None:
    """验证公开流只输出 canonical NDJSON，并在可见终结事件后停止读取。"""

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
    """验证 cursor 已消费终结事件时不再读取 sink，也不生成空行或心跳文本。"""

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
    """验证指向 internal 事件的公开 cursor 被拒绝，且拒绝前不触发读取或 stdout。"""

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
    """验证 cursor 缺口和其他 run 的序号统一映射为不可见错误，避免泄露真实存在性。"""

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
    """验证 stdout 写入被中断后不预取下一页，避免多读未实际交付的事件。"""

    runtime = _runtime([])
    runtime.sink = cast(Any, _OneEventPageSink([_event(1), _event(2, terminal=True)]))

    def interrupting_writer(_line: str) -> None:
        """模拟终端写入中断，使流处理在第一页第一条事件处停止。"""

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
    """验证空闲轮询被中断时不生成合成输出，保留客户端对真实事件的严格期望。"""

    runtime = _runtime([])
    lines: list[str] = []

    async def interrupt_sleep(_seconds: float) -> None:
        """模拟轮询等待被用户中断，而非返回一段伪造的空闲事件。"""

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
    """验证非法 cursor 在运行时构造前给出稳定错误，因而不存在需释放的资源。"""

    runtime = _runtime([_event(1, terminal=True)])

    def build_runtime(**_kwargs: Any) -> Any:
        """替换 CLI 运行时工厂，返回可观察关闭状态的内存替身。"""

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
    """验证历史损坏事件映射为稳定 stderr 错误，并在失败后释放运行时资源。"""

    runtime = _runtime([])
    runtime.sink = cast(Any, _InvalidLegacySink([]))

    def build_runtime(**_kwargs: Any) -> Any:
        """替换 CLI 运行时工厂，使命令读取损坏事件替身。"""

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
    """验证 internal 流的策略拒绝不输出任何事件内容，并仍关闭已构造运行时。"""

    runtime = _runtime([_event(1, visibility="internal")], deny_internal=True)

    def build_runtime(**_kwargs: Any) -> Any:
        """替换 CLI 运行时工厂，使命令走配置为拒绝的策略替身。"""

        return runtime

    monkeypatch.setattr(cli_events, "_build_event_stream_runtime", build_runtime)

    result = CliRunner().invoke(app, ["events", "stream", "run-1", "--include-internal"])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "policy.denied" in result.stderr
    assert runtime.storage.closed is True


def test_cli_command_help_keeps_after_seq_cli_only() -> None:
    """验证 CLI 帮助暴露本地 cursor 参数，不混入 HTTP SSE 专用请求头术语。"""

    result = CliRunner().invoke(app, ["events", "stream", "--help"])

    assert result.exit_code == 0
    assert "--after-seq" in result.output
    assert "Last-Event-ID" not in result.output


def test_cli_command_reads_real_local_runtime_without_business_writes(tmp_path: Path) -> None:
    """验证真实本地运行时可只读输出事件，且不会改写数据库或 JSONL 业务事实。"""

    async def seed() -> tuple[str, Path]:
        """创建一个已终结的真实 run，并在释放 storage 前返回其 run id 与事件文件。"""

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
