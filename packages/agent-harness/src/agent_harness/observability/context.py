"""观测 trace/span 关联字段的 provider-neutral DTO。"""

from __future__ import annotations

from typing import Any

from agent_harness.contracts.dto import HarnessDTO


class TelemetryContext(HarnessDTO):
    """runtime、tool、model、retrieval、eval、approval、audit 共用的关联上下文。"""

    tenant_id: str
    user_id: str | None = None
    agent_id: str | None = None
    run_id: str | None = None
    session_id: str | None = None
    request_id: str | None = None
    trace_id: str | None = None
    span_id: str | None = None
    parent_span_id: str | None = None
    tool_name: str | None = None
    model_provider: str | None = None
    model_name: str | None = None
    retrieval_provider: str | None = None
    eval_run_id: str | None = None

    def with_fields(self, **fields: Any) -> TelemetryContext:
        """返回带额外适用字段的新 context，不伪造调用方没有传入的关联。"""

        return self.model_copy(
            update={key: value for key, value in fields.items() if value is not None}
        )
