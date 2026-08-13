"""CLI 来源与持久化 execution context 的唯一私有 seam。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, cast

from agent_harness.contracts.dto import HarnessDTO
from agent_harness.runtime.executor import AgentExecutionContext
from agent_harness.storage.run_repositories import RunExecutionContextRecord

PROVENANCE_SCHEMA_VERSION = "run-input-provenance-v1"


class RunInputProvenance(HarnessDTO):
    """受信 CLI 来源；类型只在内部下划线模块中定义。"""

    source: Literal["cli"]


@dataclass(frozen=True, slots=True)
class ClassifiedExecutionContext:
    """一次分类后供恢复编排复用的私有 context 绑定结果。"""

    payload: dict[str, Any] | None
    provenance: RunInputProvenance | None
    authoritative_request_id: str | None


def _invalid() -> ValueError:
    return ValueError("execution_context.provenance_invalid")


def classify_execution_context_record(
    record: RunExecutionContextRecord,
) -> ClassifiedExecutionContext:
    """只解释 exact envelope；缺失 envelope 表示合法 legacy/非 CLI。"""

    raw_context = record.execution_context
    if not isinstance(raw_context, dict):
        raise _invalid()
    payload = cast(dict[str, Any], raw_context)
    if "provenance" in payload:
        raise _invalid()
    request_id = payload.get("request_id")
    if request_id is not None and (not isinstance(request_id, str) or not request_id):
        raise _invalid()
    if "input_provenance" not in payload:
        provenance = None
    else:
        raw_provenance = payload["input_provenance"]
        if not isinstance(raw_provenance, dict):
            raise _invalid()
        provenance_payload = cast(dict[str, object], raw_provenance)
        if set(provenance_payload) != {
            "schema_version",
            "source",
            "execution_request_id",
        }:
            raise _invalid()
        execution_request_id = provenance_payload.get("execution_request_id")
        if execution_request_id is not None and (
            not isinstance(execution_request_id, str) or not execution_request_id
        ):
            raise _invalid()
        if (
            provenance_payload.get("schema_version") != PROVENANCE_SCHEMA_VERSION
            or provenance_payload.get("source") != "cli"
            or execution_request_id != request_id
        ):
            raise _invalid()
        provenance = RunInputProvenance(source="cli")
    return ClassifiedExecutionContext(
        payload=payload,
        provenance=provenance,
        authoritative_request_id=request_id,
    )


def execution_context_payload(
    *,
    identity: dict[str, Any],
    request_id: str | None,
    trace_id: str,
    checkpoint_state: dict[str, Any] | None,
    provenance: RunInputProvenance | None,
) -> dict[str, Any]:
    """把受信 typed 来源投影到既有私有 JSON，不改变公开 DTO。"""

    return {
        "identity": identity,
        "request_id": request_id,
        "trace_id": trace_id,
        "checkpoint_state": checkpoint_state,
        **(
            {
                "input_provenance": {
                    "schema_version": PROVENANCE_SCHEMA_VERSION,
                    "source": provenance.source,
                    "execution_request_id": request_id,
                }
            }
            if provenance is not None
            else {}
        ),
    }


def bind_execution_provenance(
    context: AgentExecutionContext,
    provenance: RunInputProvenance | None,
) -> AgentExecutionContext:
    """只在进程内绑定 typed 来源；不会进入 context 的公开序列化形状。"""

    context._input_provenance = provenance  # pyright: ignore[reportPrivateUsage]
    return context


def execution_provenance(context: AgentExecutionContext) -> RunInputProvenance | None:
    """读取已分类的进程内来源，拒绝任意对象伪造 typed provenance。"""

    value = context._input_provenance  # pyright: ignore[reportPrivateUsage]
    return value if isinstance(value, RunInputProvenance) else None


__all__ = [
    "ClassifiedExecutionContext",
    "RunInputProvenance",
    "bind_execution_provenance",
    "classify_execution_context_record",
    "execution_context_payload",
    "execution_provenance",
]
