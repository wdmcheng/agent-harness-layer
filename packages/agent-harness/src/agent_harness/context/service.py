"""管理 ContextAssembler 的 UoW 与 artifact 生命周期。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from agent_harness.artifacts import FileArtifactStore
from agent_harness.context.assembler import (
    ContextAssembler,
    ContextAssemblyResult,
    ContextFragment,
    ContextFragmentTrace,
    context_assembly_output_digest,
)
from agent_harness.storage import SQLAlchemyStorage


class ContextAssemblyService:
    """让业务 agent 使用公共组装 seam，而不接触 ORM session/repository。"""

    def __init__(self, *, storage: SQLAlchemyStorage, artifact_store: FileArtifactStore) -> None:
        """注入事务边界与 artifact 存储，确保组装内容及其证据引用成对持久化。"""

        self._storage = storage
        self._artifact_store = artifact_store

    async def assemble(
        self,
        *,
        tenant_id: str,
        run_id: str | None,
        fragments: list[ContextFragment],
        token_budget: int,
        loop_id: str | None = None,
        turn_ordinal: int | None = None,
        tool_call_id: str | None = None,
    ) -> ContextAssemblyResult:
        """组装并持久化实际模型输入 evidence，再提交单一 UoW。"""

        async with self._storage.uow() as uow:
            result = await ContextAssembler(uow.context_assemblies).assemble(
                tenant_id=tenant_id,
                run_id=run_id,
                fragments=fragments,
                token_budget=token_budget,
                loop_id=loop_id,
                turn_ordinal=turn_ordinal,
                tool_call_id=tool_call_id,
                # 组装结果产生前还没有 output artifact；placeholder 不会提交，
                # 同一事务内会被预算裁剪后的真实 evidence ref 替换。
                output_ref="pending://context-assembly-output",
            )
            evidence = self._artifact_store.write_json(
                {
                    "kind": "context-assembly-output",
                    "assembly_id": result.id,
                    "run_id": run_id,
                    "assembled_text": result.assembled_text,
                    "retained_fragments": [
                        fragment.to_payload() for fragment in result.retained_fragments
                    ],
                    "fragment_traces": [trace.to_payload() for trace in result.fragment_traces],
                    "truncation_summary": result.truncation_summary,
                    "fallback_decision": result.fallback_decision,
                }
            )
            persisted = await uow.context_assemblies.update_output_ref(
                result.id,
                output_ref=evidence.ref,
            )
            await uow.commit()
        return result.model_copy(update={"output_ref": persisted.output_ref})

    async def replay_loop_turn(
        self,
        *,
        tenant_id: str,
        run_id: str,
        loop_id: str,
        turn_ordinal: int,
    ) -> ContextAssemblyResult:
        """从唯一loop-turn记录与内容寻址artifact恢复exact安全模型输入。"""

        async with self._storage.uow() as uow:
            record = await uow.context_assemblies.get_by_loop_turn(
                tenant_id=tenant_id,
                loop_id=loop_id,
                turn_ordinal=turn_ordinal,
            )
        if (
            record is None
            or record.run_id != run_id
            or record.tool_call_id is None
            or record.output_digest is None
            or record.output_ref == "pending://context-assembly-output"
        ):
            raise RuntimeError("context.assembly_replay_conflict")
        try:
            payload = self._artifact_store.read_json(record.output_ref)
            if set(payload) != {
                "kind",
                "assembly_id",
                "run_id",
                "assembled_text",
                "retained_fragments",
                "fragment_traces",
                "truncation_summary",
                "fallback_decision",
            }:
                raise ValueError("context assembly artifact shape is invalid")
            if (
                payload["kind"] != "context-assembly-output"
                or payload["assembly_id"] != record.id
                or payload["run_id"] != run_id
                or not isinstance(payload["assembled_text"], str)
                or not isinstance(payload["retained_fragments"], list)
                or not isinstance(payload["fragment_traces"], list)
                or not isinstance(payload["truncation_summary"], dict)
            ):
                raise ValueError("context assembly artifact identity is invalid")
            retained_payload = cast(list[object], payload["retained_fragments"])
            trace_payload = cast(list[object], payload["fragment_traces"])
            truncation_payload = cast(Mapping[object, object], payload["truncation_summary"])
            retained = [ContextFragment.model_validate(item) for item in retained_payload]
            traces = [ContextFragmentTrace.model_validate(item) for item in trace_payload]
            truncation = {
                str(key): int(value)
                for key, value in truncation_payload.items()
                if type(value) is int
            }
            if (
                truncation != record.truncation_summary
                or context_assembly_output_digest(
                    assembled_text=payload["assembled_text"],
                    retained_fragments=retained,
                    fragment_traces=traces,
                    truncation_summary=truncation,
                )
                != record.output_digest
            ):
                raise ValueError("context assembly artifact digest is invalid")
            return ContextAssemblyResult(
                id=record.id,
                output_ref=record.output_ref,
                input_refs=record.input_refs,
                token_budget=record.token_budget,
                trust_summary=record.trust_summary,
                truncation_summary=record.truncation_summary,
                assembled_text=payload["assembled_text"],
                fallback_decision=payload["fallback_decision"],
                retained_fragments=retained,
                fragment_traces=traces,
            )
        except Exception:
            raise RuntimeError("context.assembly_replay_conflict") from None
