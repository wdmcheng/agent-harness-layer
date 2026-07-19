"""CanonicalEvent 的只读 CLI stream adapter。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer

from agent_harness import cli_shared
from agent_harness.events import (
    EventReader,
    LocalJsonlEventSink,
    PostgreSQLEventSink,
    canonical_event_bytes,
)
from agent_harness.events.serialization import CanonicalEventEnvelopeStateInvalid
from agent_harness.identity import IdentityContext
from agent_harness.policy import PolicyCheck, PolicyDeniedError, PolicyEngine
from agent_harness.runtime import authorize_run_read
from agent_harness.storage import SQLAlchemyStorage, storage_dsn_from_settings

MAX_EVENT_SEQ = 2_147_483_647
_POLL_INTERVAL_SECONDS = 0.25

events_app = typer.Typer(no_args_is_help=True)


class CliEventStreamError(RuntimeError):
    """CLI stream 的稳定脱敏错误；不携带 event、payload 或内部异常。"""

    def __init__(self, code: str, message: str) -> None:
        """保存面向 CLI 的稳定错误码，禁止原始 sink 异常直接穿透。"""

        super().__init__(message)
        self.code = code


@dataclass(slots=True)
class EventStreamRuntime:
    """一次 CLI stream 使用的只读依赖，生命周期由命令入口统一释放。"""

    storage: SQLAlchemyStorage
    sink: EventReader
    policy: PolicyEngine
    identity: IdentityContext

    async def close(self) -> None:
        """释放只读 stream 使用的 storage 连接；sink 本身不拥有额外生命周期。"""

        await self.storage.dispose()


def _parse_after_seq(raw: str) -> int:
    """解析受限范围内的十进制 exclusive cursor，拒绝符号、空白和溢出输入。"""

    if not raw.isdecimal():
        raise CliEventStreamError("validation_error", "after-seq must be a decimal integer")
    value = int(raw)
    if value > MAX_EVENT_SEQ:
        raise CliEventStreamError("validation_error", "after-seq is outside the supported range")
    return value


def _build_event_stream_runtime(
    *,
    profile: str,
    profiles_dir: Path | None,
    storage_dsn: str | None,
    events_path: Path | None,
) -> EventStreamRuntime:
    """按 profile 组装唯一 EventSink；不会构造 EventBus 或写入 audit/evidence。"""

    settings = cli_shared.load_settings_or_exit(profile, profiles_dir)
    resolved_dsn = storage_dsn or storage_dsn_from_settings(settings)
    cli_shared.require_schema_or_exit(resolved_dsn)
    storage = SQLAlchemyStorage.from_dsn(resolved_dsn)
    if settings.profile == "service":
        sink: EventReader = PostgreSQLEventSink(storage)
    else:
        resolved_events_path = cli_shared.event_path(settings, events_path)
        cli_shared.require_local_state_ready_or_exit(event_paths=(resolved_events_path,))
        sink = LocalJsonlEventSink(resolved_events_path)
    return EventStreamRuntime(
        storage=storage,
        sink=sink,
        # Stream 是纯读取。这里复用同一 provider/identity，但不写 policy audit。
        policy=cli_shared.policy_engine(settings, storage, None, profiles_dir=profiles_dir),
        identity=settings.identity.default,
    )


async def _authorize_stream(
    runtime: EventStreamRuntime,
    *,
    run_id: str,
    include_internal: bool,
) -> None:
    """先校验 tenant ownership 与 internal policy，失败时不触碰 EventSink。"""

    try:
        await authorize_run_read(
            runtime.storage,
            run_id=run_id,
            identity=runtime.identity,
        )
    except LookupError as exc:
        raise CliEventStreamError("api.not_found", "run not found") from exc
    if include_internal:
        await runtime.policy.require_allowed_readonly(
            PolicyCheck(
                actor=runtime.identity,
                action="events.read_internal",
                resource=f"run:{run_id}:events",
                context={"include_internal": True, "source": "cli"},
            )
        )


async def stream_event_lines(
    runtime: EventStreamRuntime,
    *,
    run_id: str,
    after_seq: int,
    include_internal: bool,
    write_line: Callable[[str], None],
) -> None:
    """逐条写 canonical NDJSON；每次只读取并消费一个受限 page。

    ``write_line`` 是 stdout 背压边界：当前行返回前不会读取下一条 page。空闲
    轮询不输出 heartbeat；terminal 已消费或刚输出后立即收口。
    """

    await _authorize_stream(runtime, run_id=run_id, include_internal=include_internal)
    if after_seq and not await runtime.sink.contains_seq(
        run_id=run_id,
        seq=after_seq,
        include_internal=include_internal,
    ):
        raise CliEventStreamError("validation_error", "after-seq is not visible")
    terminal = await runtime.sink.terminal_event(
        run_id=run_id,
        include_internal=include_internal,
    )
    if terminal is not None and after_seq == terminal.seq:
        return

    cursor = after_seq
    while True:
        page = await runtime.sink.read_page(
            run_id=run_id,
            after_seq=cursor,
            include_internal=include_internal,
        )
        if page:
            for event in page:
                write_line(canonical_event_bytes(event).decode("utf-8"))
                cursor = event.seq
                if event.terminal:
                    return
            continue
        terminal = await runtime.sink.terminal_event(
            run_id=run_id,
            include_internal=include_internal,
        )
        if terminal is not None and cursor >= terminal.seq:
            return
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)


@events_app.command("stream")
def stream_events(
    run_id: str,
    profile: Annotated[str, typer.Option("--profile")] = "local",
    profiles_dir: Annotated[Path | None, typer.Option("--profiles-dir")] = None,
    storage_dsn: Annotated[str | None, typer.Option("--storage-dsn")] = None,
    events_path: Annotated[Path | None, typer.Option("--events-path")] = None,
    after_seq: Annotated[str, typer.Option("--after-seq")] = "0",
    include_internal: Annotated[bool, typer.Option("--include-internal")] = False,
) -> None:
    """按 exclusive cursor 逐条输出当前身份可见的 CanonicalEvent NDJSON。"""

    runtime: EventStreamRuntime | None = None
    try:
        parsed_after_seq = _parse_after_seq(after_seq)
        runtime = _build_event_stream_runtime(
            profile=profile,
            profiles_dir=profiles_dir,
            storage_dsn=storage_dsn,
            events_path=events_path,
        )
        asyncio.run(
            stream_event_lines(
                runtime,
                run_id=run_id,
                after_seq=parsed_after_seq,
                include_internal=include_internal,
                write_line=typer.echo,
            )
        )
    except PolicyDeniedError as exc:
        typer.echo(f"{exc.code}: internal event access denied", err=True)
        raise typer.Exit(1) from exc
    except CanonicalEventEnvelopeStateInvalid as exc:
        typer.echo("stream.event_state_invalid: persisted event is invalid", err=True)
        raise typer.Exit(1) from exc
    except CliEventStreamError as exc:
        typer.echo(f"{exc.code}: {exc}", err=True)
        raise typer.Exit(1) from exc
    except KeyboardInterrupt as exc:
        # Ctrl-C 不打印合成 event 或提示；130 与常见 shell SIGINT 语义一致。
        raise typer.Exit(130) from exc
    finally:
        if runtime is not None:
            asyncio.run(runtime.close())


def register_event_commands(root_app: typer.Typer) -> None:
    """把 events 命令组注册到核心 CLI。"""

    root_app.add_typer(events_app, name="events")


__all__ = [
    "CliEventStreamError",
    "EventStreamRuntime",
    "events_app",
    "register_event_commands",
    "stream_event_lines",
]
