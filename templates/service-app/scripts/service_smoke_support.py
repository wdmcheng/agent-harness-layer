"""Service smoke 的 Compose、进程与隔离 wheel 辅助函数。"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Any, cast

APP_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = APP_ROOT / "docker-compose.yml"


def last_json_line(output: str) -> dict[str, Any]:
    for line in reversed(output.splitlines()):
        if line.lstrip().startswith("{"):
            return cast(dict[str, Any], json.loads(line))
    raise RuntimeError("service admin returned no JSON evidence")


def free_port() -> int:
    """由内核分配本机临时端口，避免并发 smoke 使用固定端口。"""

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return cast(int, listener.getsockname()[1])


def run(
    command: list[str],
    *,
    env: dict[str, str],
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=APP_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=check,
    )


def _compose_command(env: dict[str, str], *args: str) -> list[str]:
    return [
        "docker",
        "compose",
        "-f",
        str(COMPOSE_FILE),
        "-p",
        env["SERVICE_APP_COMPOSE_PROJECT"],
        "--profile",
        "service",
        *args,
    ]


def compose(env: dict[str, str], *args: str, check: bool = True) -> str:
    """只操作当前随机 Compose project，禁止使用全局 Docker 清理。"""

    command = _compose_command(env, *args)
    try:
        result = run(command, env=env, check=check)
    except subprocess.CalledProcessError as exc:
        message = failure_diagnostic(
            f"compose-{args[0]}",
            env,
            raw_detail=exc.stderr or exc.stdout,
            exit_code=exc.returncode,
        )
        raise RuntimeError(message) from None
    return result.stdout.strip()


def compose_result(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    """返回隔离 Compose 命令结果，供预期失败合同检查退出码与脱敏边界。"""

    return run(_compose_command(env, *args), env=env, check=False)


def cleanup_project(env: dict[str, str], *, preserve_volume: bool) -> None:
    """有限重试并逐项证明本轮 container/network/默认 volume 已清理。"""

    project = env["SERVICE_APP_COMPOSE_PROJECT"]
    network = f"{project}_default"
    volume = f"{project}_agent_harness_postgres_data"
    down_args = ["down", "--remove-orphans"]
    if not preserve_volume:
        down_args.append("-v")
    for _ in range(20):
        run(_compose_command(env, *down_args), env=env, check=False)
        containers = run(
            [
                "docker",
                "ps",
                "-aq",
                "--filter",
                f"label=com.docker.compose.project={project}",
            ],
            env=env,
            check=False,
        ).stdout.split()
        if containers:
            run(["docker", "rm", "-f", *containers], env=env, check=False)
        run(["docker", "network", "rm", network], env=env, check=False)
        if not preserve_volume:
            run(["docker", "volume", "rm", volume], env=env, check=False)

        containers_left = run(
            [
                "docker",
                "ps",
                "-aq",
                "--filter",
                f"label=com.docker.compose.project={project}",
            ],
            env=env,
            check=False,
        ).stdout.strip()
        network_left = run(["docker", "network", "inspect", network], env=env, check=False)
        volume_left = run(["docker", "volume", "inspect", volume], env=env, check=False)
        volume_ok = preserve_volume or volume_left.returncode != 0
        if not containers_left and network_left.returncode != 0 and volume_ok:
            return
        time.sleep(0.25)
    raise RuntimeError(failure_diagnostic("cleanup", env))


def failure_diagnostic(
    boundary: str,
    env: dict[str, str],
    *,
    raw_detail: str | None = None,
    exit_code: int | None = None,
) -> str:
    """只输出稳定边界与隔离 project；原始 provider/Compose 内容一律不回显。"""

    del raw_detail
    safe_boundary = re.sub(r"[^A-Za-z0-9_.-]", "-", boundary)
    project = re.sub(
        r"[^A-Za-z0-9_.-]",
        "-",
        env.get("SERVICE_APP_COMPOSE_PROJECT", "unknown"),
    )
    suffix = "" if exit_code is None else f" exit={exit_code}"
    return (
        f"smoke-service: failure boundary={safe_boundary} project={project}{suffix} detail=redacted"
    )


def preserve_postgres_volume(
    requested: bool,
    *,
    credential_cleanup_confirmed: bool,
) -> bool:
    """只有显式请求且 credential 已确认删除时才允许保留 PostgreSQL 数据。"""

    return requested and credential_cleanup_confirmed


def postgres_counts(env: dict[str, str]) -> tuple[int, int]:
    """读取认证零副作用断言所需的 run/audit 计数。"""

    output = compose(
        env,
        "exec",
        "-T",
        "postgres",
        "psql",
        "-U",
        "agent_harness",
        "-d",
        "agent_harness",
        "-At",
        "-c",
        "select (select count(*) from agent_runs), (select count(*) from audit_logs);",
    )
    run_count, audit_count = output.split("|")
    return int(run_count), int(audit_count)


def redis_json(env: dict[str, str], *args: str) -> object:
    output = compose(env, "exec", "-T", "redis", "redis-cli", "--json", *args)
    return json.loads(output)


def stream_length(env: dict[str, str], stream: str) -> int:
    return int(compose(env, "exec", "-T", "redis", "redis-cli", "XLEN", stream))


def first_stream_message(env: dict[str, str], stream: str) -> tuple[str, dict[str, Any]]:
    rows = cast(list[list[object]], redis_json(env, "XRANGE", stream, "-", "+", "COUNT", "1"))
    message_id = cast(str, rows[0][0])
    fields = cast(list[str], rows[0][1])
    payload = fields[fields.index("payload") + 1]
    return message_id, cast(dict[str, Any], json.loads(payload))


def run_queue_ack_evidence(
    env: dict[str, str], *, stream: str, group: str, run_id: str
) -> dict[str, int]:
    """在 durable evidence 可见后证明该 run 的 Redis delivery 已全部确认。"""

    rows = cast(list[list[object]], redis_json(env, "XRANGE", stream, "-", "+"))
    message_ids: list[str] = []
    for raw_message_id, raw_fields in rows:
        fields = cast(list[str], raw_fields)
        payload = cast(dict[str, Any], json.loads(fields[fields.index("payload") + 1]))
        if payload.get("run_id") == run_id:
            message_ids.append(cast(str, raw_message_id))
    pending = cast(list[list[object]], redis_json(env, "XPENDING", stream, group, "-", "+", "100"))
    pending_ids = {cast(str, item[0]) for item in pending}
    if not message_ids or pending_ids.intersection(message_ids):
        raise RuntimeError("worker delivery remained pending after durable run evidence")
    return {"messages": len(message_ids), "pending": 0}


def run_queue_pending_evidence(
    env: dict[str, str], *, stream: str, group: str, run_id: str
) -> dict[str, int]:
    """证明应用 evidence 已 durable 时，对应 Redis delivery 仍等待 ack/reclaim。"""

    rows = cast(list[list[object]], redis_json(env, "XRANGE", stream, "-", "+"))
    message_ids: list[str] = []
    for raw_message_id, raw_fields in rows:
        fields = cast(list[str], raw_fields)
        payload = cast(dict[str, Any], json.loads(fields[fields.index("payload") + 1]))
        if payload.get("run_id") == run_id:
            message_ids.append(cast(str, raw_message_id))
    pending = cast(list[list[object]], redis_json(env, "XPENDING", stream, group, "-", "+", "100"))
    pending_ids = {cast(str, item[0]) for item in pending}
    matching = pending_ids.intersection(message_ids)
    if not message_ids or not matching:
        raise RuntimeError("approval delivery was acked before the crash boundary was observed")
    return {"messages": len(message_ids), "pending": len(matching)}


def install_approval_event_write_failure(env: dict[str, str]) -> None:
    """用隔离数据库 trigger 制造一次真实 approval event 写前失败。"""

    statement = """
    create or replace function smoke_fail_approval_event_write() returns trigger
    language plpgsql as $smoke$
    begin
      if new.event_type = 'approval.resolved' then
        raise exception 'isolated smoke approval event write failure';
      end if;
      return new;
    end;
    $smoke$;
    drop trigger if exists smoke_fail_approval_event_write on canonical_events;
    create trigger smoke_fail_approval_event_write before insert on canonical_events
    for each row execute function smoke_fail_approval_event_write();
    """
    compose(
        env,
        "exec",
        "-T",
        "postgres",
        "psql",
        "-v",
        "ON_ERROR_STOP=1",
        "-U",
        "agent_harness",
        "-d",
        "agent_harness",
        "-c",
        statement,
    )


def remove_approval_event_write_failure(env: dict[str, str]) -> None:
    """删除本轮隔离 trigger，恢复正常 event writer。"""

    compose(
        env,
        "exec",
        "-T",
        "postgres",
        "psql",
        "-v",
        "ON_ERROR_STOP=1",
        "-U",
        "agent_harness",
        "-d",
        "agent_harness",
        "-c",
        "drop trigger if exists smoke_fail_approval_event_write on canonical_events; "
        "drop function if exists smoke_fail_approval_event_write();",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="验证真实 service Compose 边界")
    parser.add_argument("--migrate-only", action="store_true")
    return parser.parse_args()


def cleanup_credential(env: dict[str, str], token: str, *, check: bool = True) -> bool:
    """通过容器内公开 repository seam 删除本轮临时 credential。"""

    cleanup_env = {**env, "SERVICE_APP_BOOTSTRAP_TOKEN": token}
    output = compose(
        cleanup_env,
        "run",
        "--rm",
        "-e",
        "SERVICE_APP_BOOTSTRAP_TOKEN",
        "migration",
        "python",
        "scripts/service_admin.py",
        "cleanup-credential",
        check=check,
    )
    try:
        return bool(last_json_line(output).get("credential_deleted"))
    except (json.JSONDecodeError, RuntimeError):
        if check:
            raise
        return False


def cleanup_credential_at_boundary(
    env: dict[str, str],
    token: str,
    *,
    check: bool = True,
) -> bool:
    """先切换到 cleanup 边界，再删除临时 credential 或执行静默重试。"""

    env["SERVICE_APP_SMOKE_BOUNDARY"] = "cleanup"
    return cleanup_credential(env, token, check=check)


def assert_stale_receipt(
    env: dict[str, str],
    *,
    stream: str,
    group: str,
    message_id: str,
    consumer_id: str,
    delivery_count: int,
) -> bool:
    """在真实 Compose 网络内用旧 receipt 调用 queue adapter ack seam。"""

    output = compose(
        env,
        "run",
        "--rm",
        "migration",
        "python",
        "scripts/service_admin.py",
        "assert-stale-receipt",
        stream,
        group,
        message_id,
        consumer_id,
        str(delivery_count),
    )
    return last_json_line(output).get("stale_receipt_rejected") is True


def inspect_run(env: dict[str, str], run_id: str) -> dict[str, Any]:
    result = compose_result(
        env,
        "run",
        "--rm",
        "migration",
        "python",
        "scripts/service_admin.py",
        "inspect-run",
        run_id,
    )
    payload = last_json_line(result.stdout)
    if result.returncode != 0:
        error_type = re.sub(r"[^A-Za-z0-9_.-]", "-", str(payload.get("error_type")))
        error_code = re.sub(r"[^A-Za-z0-9_.-]", "-", str(payload.get("error_code")))
        raise RuntimeError(f"service.inspect.{error_type}.{error_code}")
    return payload


def postgres_terminal_evidence(
    expected: dict[str, str],
    completed: dict[str, Any],
    *,
    workflow_id: str,
) -> dict[str, object]:
    """核对 model usage、容量结算、执行上下文与唯一终态事件。"""

    terminals = [event for event in completed["events"] if event["terminal"]]
    started = [event for event in completed["events"] if event["type"] == "model.request.started"]
    usages = [event for event in completed["events"] if event["type"] == "model.usage.updated"]
    terminal = terminals[0] if len(terminals) == 1 else None
    model_started = started[0] if len(started) == 1 else None
    usage = usages[0] if len(usages) == 1 else None
    started_call_id = (
        None
        if model_started is None
        else model_started.get("payload", {}).get("correlation", {}).get("usage_call_id")
    )
    usage_call_id = (
        None
        if usage is None
        else usage.get("payload", {}).get("correlation", {}).get("usage_call_id")
    )
    usage_outbox = [
        item
        for item in completed["outbox"]
        if item["operation_kind"] == "model_usage" and item["usage_call_id"] == usage_call_id
    ]
    capacity = completed["capacity"]
    checks = {
        "terminal_count": terminal is not None,
        "model_started_count": model_started is not None,
        "usage_count": usage is not None,
        "usage_call_id": isinstance(usage_call_id, str) and started_call_id == usage_call_id,
        "usage_order": (
            model_started is not None
            and usage is not None
            and terminal is not None
            and model_started["seq"] < usage["seq"] < terminal["seq"]
            and not usage["terminal"]
        ),
        "usage_outbox": (
            usage is not None
            and len(usage_outbox) == 1
            and usage_outbox[0]["state"] == "published"
            and usage_outbox[0]["event_id"] == usage["event_id"]
        ),
        "capacity": (
            terminal is not None
            and capacity["highest_persisted_seq"] == terminal["seq"]
            and capacity["outstanding_reserved_event_count"] == 0
            and capacity["terminal_reservation"] == 0
        ),
        "workflow": completed["workflow_id"] == workflow_id,
        "correlation": not any(completed.get(key) != value for key, value in expected.items()),
        "terminal_shape": (
            terminal is not None
            and terminal["type"] == "run.completed"
            and terminal["visibility"] == "public"
            and terminal["request_id"] == expected["request_id"]
            and bool(terminal["event_id"])
            and terminal["trace_id"] == completed.get("trace_id")
        ),
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise RuntimeError("service.evidence." + ",".join(failed))
    assert terminal is not None
    assert model_started is not None
    assert usage is not None
    assert len(usage_outbox) == 1
    return {
        "execution": {key: completed[key] for key in expected},
        "terminal_event": {
            "event_id": terminal["event_id"],
            "type": terminal["type"],
            "request_id": terminal["request_id"],
            "trace_id": terminal["trace_id"],
        },
        "usage": {
            "usage_call_id": usage_call_id,
            "started_seq": model_started["seq"],
            "usage_seq": usage["seq"],
            "terminal_seq": terminal["seq"],
            "outbox_state": usage_outbox[0]["state"],
            "capacity": capacity,
        },
    }


def postgres_approval_evidence(
    completed: dict[str, Any], *, expected_status: str
) -> dict[str, object]:
    """证明 resolution 先于 public terminal，且 ordered outbox/capacity 已结算。"""

    resolutions = [event for event in completed["events"] if event["type"] == "approval.resolved"]
    terminals = [event for event in completed["events"] if event["terminal"]]
    resolution = resolutions[0] if len(resolutions) == 1 else None
    terminal = terminals[0] if len(terminals) == 1 else None
    ordered = [
        item
        for item in completed["outbox"]
        if item["operation_kind"] in {"approval_resolution", "run_terminal"}
    ]
    capacity = completed["capacity"]
    expected_terminal = "run.completed" if expected_status == "approved" else "run.failed"
    if (
        resolution is None
        or terminal is None
        or resolution["seq"] >= terminal["seq"]
        or terminal["type"] != expected_terminal
        or terminal["visibility"] != "public"
        or len(ordered) != 2
        or {item["state"] for item in ordered} != {"published"}
        or [item["sequence_in_group"] for item in ordered] != [1, 2]
        or len({item["group_id"] for item in ordered}) != 1
        or [item["event_id"] for item in ordered] != [resolution["event_id"], terminal["event_id"]]
        or capacity["highest_persisted_seq"] != terminal["seq"]
        or capacity["outstanding_reserved_event_count"] != 0
        or capacity["terminal_reservation"] != 0
    ):
        raise RuntimeError("approval ordered evidence did not settle before public terminal")
    return {
        "resolution_event_id": resolution["event_id"],
        "resolution_seq": resolution["seq"],
        "terminal_event_id": terminal["event_id"],
        "terminal_seq": terminal["seq"],
        "outbox_group": ordered[0]["group_id"],
        "outbox_state": "published",
        "capacity": capacity,
    }


def reclaim_receipts_match(
    expected_message_id: str,
    worker_a: dict[str, Any],
    worker_b: dict[str, Any],
) -> bool:
    """核对两份真实 worker receipt 的同 entry、owner 变化与 delivery 递增。"""

    return (
        worker_a.get("stream") == "agent-harness:service:runs:stream"
        and worker_a.get("group") == "agent-harness-workers"
        and worker_a.get("message_id") == expected_message_id
        and worker_a.get("delivery_count") == 1
        and worker_b.get("stream") == worker_a.get("stream")
        and worker_b.get("group") == worker_a.get("group")
        and worker_b.get("message_id") == worker_a.get("message_id")
        and worker_b.get("consumer_id") != worker_a.get("consumer_id")
        and worker_b.get("delivery_count") == 2
    )


def prepare_core_wheel() -> None:
    """保证镜像只能从标准 wheel 入口安装 core，不能读取 workspace source。"""

    existing = list((APP_ROOT / ".agent-harness").glob("agent_harness-*.whl"))
    if len(existing) == 1:
        return
    source_value = os.environ.get("AGENT_HARNESS_SOURCE", "").strip()
    source = Path(source_value).expanduser().resolve() if source_value else None
    if source is None or not source.is_file() or source.suffix != ".whl":
        raise RuntimeError(
            "smoke-service requires .agent-harness/agent_harness-*.whl or "
            "AGENT_HARNESS_SOURCE=/path/to/agent_harness-0.1.0.whl"
        )
    target = APP_ROOT / ".agent-harness" / source.name
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
