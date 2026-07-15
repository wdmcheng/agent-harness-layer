"""Service smoke 的隔离 credential bootstrap 与持久化状态读取入口。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os

from agent_harness.adapters.queue import RedisRunQueue
from agent_harness.auth import hash_token
from agent_harness.config import load_settings
from agent_harness.events import PostgreSQLEventSink
from agent_harness.runtime import QueueReceipt, StaleQueueReceiptError
from agent_harness.storage import ApiKeyCreate, SQLAlchemyStorage, storage_dsn_from_settings


def storage_dsn() -> str:
    """通过 typed loader 读取 direct env 或受控 secret file 的 storage DSN。"""

    profile = os.environ.get("AGENT_HARNESS_PROFILE", "service")
    return storage_dsn_from_settings(load_settings(profile=profile))


def queue_dsn() -> str:
    value = os.environ.get("AGENT_HARNESS_QUEUE__DSN", "").strip()
    if not value:
        raise RuntimeError("AGENT_HARNESS_QUEUE__DSN is required")
    return value


async def bootstrap() -> dict[str, object]:
    token = os.environ.get("SERVICE_APP_BOOTSTRAP_TOKEN", "")
    tenant_id = os.environ.get("SERVICE_APP_BOOTSTRAP_TENANT", "")
    if not token or not tenant_id:
        raise RuntimeError("isolated token and tenant are required")
    storage = SQLAlchemyStorage.from_dsn(storage_dsn())
    try:
        async with storage.uow() as uow:
            await uow.tenants.ensure(tenant_id)
            record = await uow.api_keys.create(
                ApiKeyCreate(
                    tenant_id=tenant_id,
                    user_id="service-smoke-reviewer",
                    name="service-smoke-ephemeral",
                    token_hash=hash_token(token),
                    roles=["admin", "reviewer"],
                    permissions=["*"],
                )
            )
            await uow.commit()
        return {"credential_id": record.id, "tenant_id": tenant_id}
    finally:
        await storage.dispose()


async def cleanup_credential() -> dict[str, object]:
    """在 Compose teardown 前显式删除本次 smoke 创建的临时 credential。"""

    token = os.environ.get("SERVICE_APP_BOOTSTRAP_TOKEN", "")
    if not token:
        raise RuntimeError("isolated token is required")
    storage = SQLAlchemyStorage.from_dsn(storage_dsn())
    try:
        async with storage.uow() as uow:
            deleted = await uow.api_keys.delete_by_hash(hash_token(token))
            await uow.commit()
        return {"credential_deleted": deleted}
    finally:
        await storage.dispose()


async def inspect_run(run_id: str) -> dict[str, object]:
    storage = SQLAlchemyStorage.from_dsn(storage_dsn())
    try:
        async with storage.uow() as uow:
            run = await uow.runs.get(run_id)
            private = await uow.runs.get_execution(run_id)
            approvals = await uow.approvals.list_by_run(run_id)
            checkpoint = await uow.checkpoints.get_latest(run_id)
            capacity = await uow.event_capacity.snapshot(run_id)
            outbox = await uow.evidence_outbox.list_for_run(run_id=run_id)
            outbox_evidence = [
                {
                    "event_id": item.event_id,
                    "usage_call_id": item.usage_call_id,
                    "operation_kind": item.operation_kind,
                    "state": item.state,
                    "reserved_event_count": item.reserved_event_count,
                    "group_id": item.group_id,
                    "sequence_in_group": item.sequence_in_group,
                }
                for item in outbox
            ]
            approval_evidence: list[dict[str, object]] = []
            for item in approvals:
                invocation = await uow.tool_invocations.get_by_approval_id(item.approval_id)
                approval_evidence.append(
                    {
                        "approval_id": item.approval_id,
                        "status": item.status,
                        "action": item.action,
                        "tool_invocation": (
                            None
                            if invocation is None
                            else {
                                "invocation_id": invocation.id,
                                "execution_state": invocation.execution_state,
                                "status": invocation.status,
                                "result_ref": invocation.result_ref,
                            }
                        ),
                    }
                )
        events = await PostgreSQLEventSink(storage).read(run_id=run_id)
        if run is None:
            raise LookupError(f"run not found: {run_id}")
        return {
            "run_id": run.id,
            "tenant_id": run.tenant_id,
            "status": run.status,
            "trace_id": run.trace_id,
            "owner_id": None if private is None else private.owner_id,
            "workflow_id": None if private is None else private.workflow_id,
            "operation_id": None if private is None else private.operation_id,
            "request_id": None if private is None else private.request_id,
            "idempotency_key": (None if private is None else private.effective_idempotency_key),
            "message_id": None if private is None else private.message_id,
            "checkpoint_id": None if checkpoint is None else checkpoint.id,
            "capacity": {
                "highest_persisted_seq": capacity.highest_persisted_seq,
                "outstanding_reserved_event_count": capacity.outstanding_reserved_event_count,
                "terminal_reservation": capacity.terminal_reservation,
            },
            "outbox": outbox_evidence,
            "approvals": approval_evidence,
            "events": [
                {
                    "event_id": event.event_id,
                    "type": event.event_type.value,
                    "seq": event.seq,
                    "terminal": event.terminal,
                    "visibility": event.visibility,
                    "request_id": event.request_id,
                    "trace_id": event.trace_id,
                    "payload": event.payload,
                }
                for event in events
            ],
        }
    finally:
        await storage.dispose()


async def assert_stale_receipt(
    stream: str,
    group: str,
    message_id: str,
    consumer_id: str,
    delivery_count: int,
) -> dict[str, object]:
    """通过公开 Redis queue seam 证明旧 owner receipt 无法确认。"""

    queue = RedisRunQueue.from_dsn(queue_dsn())
    try:
        try:
            await queue.ack(
                QueueReceipt(
                    stream=stream,
                    group=group,
                    message_id=message_id,
                    consumer_id=consumer_id,
                    delivery_count=delivery_count,
                )
            )
        except StaleQueueReceiptError:
            return {"stale_receipt_rejected": True}
        raise RuntimeError("old queue receipt unexpectedly acknowledged")
    finally:
        await queue.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("bootstrap")
    subparsers.add_parser("cleanup-credential")
    inspect_parser = subparsers.add_parser("inspect-run")
    inspect_parser.add_argument("run_id")
    stale_parser = subparsers.add_parser("assert-stale-receipt")
    stale_parser.add_argument("stream")
    stale_parser.add_argument("group")
    stale_parser.add_argument("message_id")
    stale_parser.add_argument("consumer_id")
    stale_parser.add_argument("delivery_count", type=int)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "bootstrap":
            payload = asyncio.run(bootstrap())
        elif args.command == "cleanup-credential":
            payload = asyncio.run(cleanup_credential())
        elif args.command == "assert-stale-receipt":
            payload = asyncio.run(
                assert_stale_receipt(
                    args.stream,
                    args.group,
                    args.message_id,
                    args.consumer_id,
                    args.delivery_count,
                )
            )
        else:
            payload = asyncio.run(inspect_run(args.run_id))
    except Exception as exc:  # noqa: BLE001 - 管理脚本只输出脱敏错误分类
        print(
            json.dumps(
                {
                    "error_code": getattr(exc, "code", None),
                    "error_type": type(exc).__name__,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 1
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
