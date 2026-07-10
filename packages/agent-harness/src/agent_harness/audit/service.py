"""跨 policy、approval 和 API 的审计写入服务。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from agent_harness.identity import IdentityContext
from agent_harness.security.redaction import redact_secrets
from agent_harness.storage import AuditLogCreate, AuditLogRecord, SQLAlchemyStorage


class AuditService:
    """审计日志的唯一业务入口，确保 tenant 与 payload redaction 一致。"""

    def __init__(self, storage: SQLAlchemyStorage) -> None:
        self._storage = storage

    async def record(
        self,
        *,
        actor: IdentityContext,
        action: str,
        resource: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> AuditLogRecord:
        """写入审计记录，并在入库前统一脱敏 payload。"""

        create = build_audit_log(
            actor=actor,
            action=action,
            resource=resource,
            payload=payload or {},
        )
        async with self._storage.uow() as uow:
            await uow.tenants.ensure(actor.tenant_id)
            record = await uow.audit_logs.create(create)
            await uow.commit()
            return record


def build_audit_log(
    *,
    actor: IdentityContext,
    action: str,
    resource: str | None,
    payload: dict[str, Any],
) -> AuditLogCreate:
    """构造已脱敏的审计 DTO，供状态事务原子追加 evidence。"""

    return AuditLogCreate(
        tenant_id=actor.tenant_id,
        actor_user_id=actor.user_id,
        action=action,
        resource=resource,
        payload=redact_secrets(
            _audit_payload(
                actor=actor,
                action=action,
                resource=resource,
                payload=payload,
            )
        ),
    )


def _audit_payload(
    *,
    actor: IdentityContext,
    action: str,
    resource: str | None,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """把不同 seam 的 payload 收敛成统一审计字段。"""

    decision = _metadata_value(payload, "decision")
    result = _metadata_value(payload, "result") or _metadata_value(payload, "status") or decision
    return {
        "tenant_id": actor.tenant_id,
        "user_id": actor.user_id,
        "session_id": actor.session_id,
        "agent_id": _metadata_value(payload, "agent_id"),
        "run_id": _metadata_value(payload, "run_id"),
        "trace_id": _metadata_value(payload, "trace_id"),
        "request_id": _metadata_value(payload, "request_id"),
        "action": action,
        "resource": resource,
        "decision": decision,
        "result": result,
        "timestamp": datetime.now(tz=UTC).isoformat(),
        "evidence": payload,
    }


def _metadata_value(payload: dict[str, Any], key: str) -> Any:
    """从常见 payload 嵌套位置提取审计关联字段。"""

    direct = payload.get(key)
    if direct is not None:
        return direct
    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        metadata_dict = cast(dict[str, Any], metadata)
        value = metadata_dict.get(key)
        if value is not None:
            return value
        context = metadata_dict.get("context")
        if isinstance(context, dict):
            context_dict = cast(dict[str, Any], context)
            value = context_dict.get(key)
            if value is not None:
                return value
    actor = payload.get("actor")
    if isinstance(actor, dict):
        actor_dict = cast(dict[str, Any], actor)
        value = actor_dict.get(key)
        if value is not None:
            return value
    context = payload.get("context")
    if isinstance(context, dict):
        context_dict = cast(dict[str, Any], context)
        value = context_dict.get(key)
        if value is not None:
            return value
    return None
