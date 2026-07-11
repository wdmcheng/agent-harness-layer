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
    return last_json_line(
        compose(
            env,
            "run",
            "--rm",
            "migration",
            "python",
            "scripts/service_admin.py",
            "inspect-run",
            run_id,
        )
    )


def postgres_terminal_evidence(
    expected: dict[str, str],
    completed: dict[str, Any],
    *,
    workflow_id: str,
) -> dict[str, object]:
    """核对持久化执行上下文与唯一终态事件，并返回可审计证据。"""

    terminals = [event for event in completed["events"] if event["terminal"]]
    terminal = terminals[0] if len(terminals) == 1 else None
    if (
        terminal is None
        or completed["workflow_id"] != workflow_id
        or any(completed.get(key) != value for key, value in expected.items())
        or terminal["type"] != "run.completed"
        or terminal["request_id"] != expected["request_id"]
        or not terminal["event_id"]
        or terminal["trace_id"] != completed.get("trace_id")
    ):
        raise RuntimeError("replacement worker did not preserve workflow/unique terminal")
    return {
        "execution": {key: completed[key] for key in expected},
        "terminal_event": {
            "event_id": terminal["event_id"],
            "type": terminal["type"],
            "request_id": terminal["request_id"],
            "trace_id": terminal["trace_id"],
        },
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
