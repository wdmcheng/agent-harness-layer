"""模型 usage 恢复合同共享的最小 durable run 与结算夹具。"""

from agent_harness.models import ModelDecision, ModelResponse, ModelUsageEvidence
from agent_harness.storage import RunCreate, SessionCreate, SQLAlchemyStorage


async def usage_run(storage: SQLAlchemyStorage) -> str:
    """创建带固定租户、会话和 trace 的最小 run，供 outbox 与容量断言复用。"""

    async with storage.uow() as uow:
        await uow.tenants.ensure("tenant-a")
        await uow.sessions.ensure(
            SessionCreate(
                session_id="session-a",
                tenant_id="tenant-a",
                user_id="user-a",
                agent_id="agent-a",
            )
        )
        run = await uow.runs.create(
            RunCreate(
                tenant_id="tenant-a",
                session_id="session-a",
                agent_id="agent-a",
                trace_id="trace-a",
            )
        )
        await uow.commit()
        return run.id


def completed_response_payload(evidence: ModelUsageEvidence) -> dict[str, object]:
    """构造与 final evidence 一致的封闭 completed response，禁止残缺夹具绕过恢复校验。"""

    token_usage = (
        {
            "input_tokens": evidence.input_tokens,
            "output_tokens": evidence.output_tokens,
        }
        if evidence.input_tokens is not None and evidence.output_tokens is not None
        else {}
    )
    return ModelResponse(
        provider=evidence.provider,
        model=evidence.model,
        output_text="recovered fixture output",
        decision=ModelDecision(
            action="call",
            estimated_tokens=(evidence.input_tokens or 0) + (evidence.output_tokens or 0),
        ),
        token_usage=token_usage,
        latency_ms=evidence.latency_ms,
        cost_usd=evidence.cost_usd,
        cost_status=evidence.cost_status,
    ).to_payload()


__all__ = ["completed_response_payload", "usage_run"]
