"""四个示例共用的 provider-neutral trace 与 service 访问辅助。"""

from __future__ import annotations

from agent_harness.observability import TelemetryContext, TelemetryFacade, TelemetryRecord
from agent_harness.runtime import AgentExecutionContext, AgentExecutionRequest


def require_service[T](context: AgentExecutionContext, name: str, service_type: type[T]) -> T:
    """取得 composition 服务，并在类型不匹配时 fail closed。"""

    service = context.require_service(name)
    if not isinstance(service, service_type):
        raise TypeError(f"agent execution service has invalid type: {name}")
    return service


async def publish_example_trace(
    *,
    context: AgentExecutionContext,
    request: AgentExecutionRequest,
    name: str,
    payload: dict[str, object],
) -> dict[str, object]:
    """通过 TelemetryFacade 写 local-first evidence，并返回稳定摘要。"""

    telemetry = require_service(context, "telemetry", TelemetryFacade)
    result = await telemetry.publish_record(
        TelemetryRecord(
            name=name,
            context=TelemetryContext(
                tenant_id=context.identity.tenant_id,
                user_id=context.identity.user_id,
                agent_id=request.agent_id,
                run_id=request.run_id,
                request_id=context.request_id,
                trace_id=context.trace_id,
            ),
            payload=payload,
        )
    )
    return {
        "trace_ref": f"local-jsonl://runs/{request.run_id}",
        "local_status": result.local_status.status,
        "provider_statuses": [status.to_payload() for status in result.provider_statuses],
    }
