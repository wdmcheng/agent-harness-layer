"""Service smoke 的审批、worker、持久化证据与故障注入场景操作。"""

from __future__ import annotations

import argparse
import json
import re
from typing import Any, cast

from service_smoke_support import (
    compose,
    compose_result,
    last_json_line,
    redis_json,
)


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
    """解析 service smoke 入口支持的迁移专用开关，不混入运行时配置。"""
    parser = argparse.ArgumentParser(description="验证真实 service Compose 边界")
    parser.add_argument("--migrate-only", action="store_true")
    return parser.parse_args()


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
    """调用隔离管理容器读取运行证据，并映射为稳定、脱敏的诊断错误。

    只有子进程成功退出时才返回完整证据；失败时错误类型和错误码会过滤为安全
    字符，供外层记录 smoke 边界，原始数据库或连接详情不应向上传播。
    """
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


__all__ = [
    "assert_stale_receipt",
    "inspect_run",
    "install_approval_event_write_failure",
    "parse_args",
    "postgres_approval_evidence",
    "remove_approval_event_write_failure",
    "run_queue_ack_evidence",
    "run_queue_pending_evidence",
]
