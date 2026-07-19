"""管理 ContextAssembler 的 UoW 与 artifact 生命周期。"""

from __future__ import annotations

from agent_harness.artifacts import FileArtifactStore
from agent_harness.context.assembler import ContextAssembler, ContextAssemblyResult, ContextFragment
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
    ) -> ContextAssemblyResult:
        """组装并持久化实际模型输入 evidence，再提交单一 UoW。"""

        async with self._storage.uow() as uow:
            result = await ContextAssembler(uow.context_assemblies).assemble(
                tenant_id=tenant_id,
                run_id=run_id,
                fragments=fragments,
                token_budget=token_budget,
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
