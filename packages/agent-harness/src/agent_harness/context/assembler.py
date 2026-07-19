"""ContextAssembler 和可解释 assembly trace。"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from agent_harness.contracts.dto import HarnessDTO
from agent_harness.storage.repositories import ContextAssemblyCreate, ContextAssemblyRepository


class ContextFragment(HarnessDTO):
    """进入模型上下文前的单个片段，必须携带来源和信任级别。"""

    source_ref: str
    trust_level: str
    content: str
    token_estimate: int
    kind: str = "generic"
    priority: int = 100
    artifact_ref: str | None = None


class ContextFragmentTrace(HarnessDTO):
    """单个片段在预算裁剪后的可解释 trace。"""

    source_ref: str
    kind: str
    trust_level: str
    original_tokens: int
    retained_tokens: int
    status: str
    artifact_ref: str | None = None
    fallback_decision: str | None = None


class ContextAssemblyResult(HarnessDTO):
    """一次上下文组装的输出、持久化记录引用和降级摘要。"""

    id: str
    output_ref: str
    input_refs: list[str]
    token_budget: int
    trust_summary: dict[str, int]
    truncation_summary: dict[str, int]
    assembled_text: str
    fallback_decision: str | None = None
    retained_fragments: list[ContextFragment]
    fragment_traces: list[ContextFragmentTrace]


@dataclass
class _WorkingFragment:
    """组装期间的可变片段工作项，保留原始顺序并记录预算裁剪决策。"""

    index: int
    fragment: ContextFragment
    retained_tokens: int
    status: str = "retained"
    fallback_decision: str | None = None

    @property
    def original_tokens(self) -> int:
        """返回输入片段的原始 token 估算值，供 trace 解释裁剪前后的差异。"""

        return self.fragment.token_estimate

    @property
    def retained_content(self) -> str:
        """根据当前保留 token 估算生成展示内容，不改变原始 fragment。"""

        if self.retained_tokens <= 0:
            return ""
        if self.retained_tokens >= self.original_tokens:
            return self.fragment.content
        return _truncate_content(self.fragment.content, self.retained_tokens, self.original_tokens)


class ContextAssembler:
    """按 token 预算组装上下文并写入可解释的持久化 trace。

    裁剪顺序是产品行为而非偶然实现细节：优先清理历史，再压缩检索或工具输出，最后才
    丢弃其他低优先级片段。每一步都会留下逐片段 trace，便于诊断模型为何看不到内容。
    """

    def __init__(self, repository: ContextAssemblyRepository) -> None:
        """绑定组装摘要仓储，使输出引用和裁剪事实可与调用 run 一同审计。"""

        self._repository = repository

    async def assemble(
        self,
        *,
        tenant_id: str,
        run_id: str | None,
        fragments: list[ContextFragment],
        token_budget: int,
        output_ref: str,
    ) -> ContextAssemblyResult:
        """组装上下文并把输入引用、信任分布和裁剪摘要写入仓储。

        输入 fragment 保持只读：结果中的 retained fragment 是副本，调用方可安全复用
        原始列表；输出文本只拼接实际保留内容，trace 则完整覆盖被丢弃和被截断片段。
        """

        working = [
            _WorkingFragment(
                index=index,
                fragment=fragment,
                retained_tokens=fragment.token_estimate,
            )
            for index, fragment in enumerate(fragments)
        ]
        _apply_budget(working, token_budget)
        retained = [
            item.fragment.model_copy(
                update={
                    "content": item.retained_content,
                    "token_estimate": item.retained_tokens,
                }
            )
            for item in working
            if item.retained_tokens > 0
        ]
        fragment_traces = [_trace_fragment(item) for item in working]
        used_tokens = sum(item.retained_tokens for item in working)
        trust_summary = dict(Counter(fragment.trust_level for fragment in fragments))
        truncation_summary = {
            "input_count": len(fragments),
            "retained_count": len(retained),
            "truncated_count": sum(1 for item in working if item.status == "truncated"),
            "dropped_count": sum(1 for item in working if item.status == "dropped"),
            "used_tokens": used_tokens,
            "fragment_count": len(fragment_traces),
        }
        record = await self._repository.create(
            ContextAssemblyCreate(
                tenant_id=tenant_id,
                run_id=run_id,
                input_refs=[fragment.source_ref for fragment in fragments],
                token_budget=token_budget,
                trust_summary=trust_summary,
                truncation_summary=truncation_summary,
                output_ref=output_ref,
            )
        )
        return ContextAssemblyResult(
            id=record.id,
            output_ref=record.output_ref,
            input_refs=record.input_refs,
            token_budget=record.token_budget,
            trust_summary=record.trust_summary,
            truncation_summary=record.truncation_summary,
            assembled_text="\n".join(fragment.content for fragment in retained),
            fallback_decision=_assembly_decision(fragment_traces),
            retained_fragments=retained,
            fragment_traces=fragment_traces,
        )


def _apply_budget(working: list[_WorkingFragment], token_budget: int) -> None:
    """按项目契约执行预算降级：先历史，再检索/工具，最后低优先级片段。"""

    if token_budget < 0:
        # 负数预算没有“反向保留”语义，统一收敛为零以保证循环可终止。
        token_budget = 0
    while _used_tokens(working) > token_budget:
        # 顺序不可调整：它决定会话历史、外部证据和普通上下文之间的稳定优先级。
        if _drop_next_history(working):
            continue
        if _truncate_next_retrieval_or_tool(working, token_budget):
            continue
        if _drop_lowest_priority_fragment(working):
            continue
        break


def _drop_next_history(working: list[_WorkingFragment]) -> bool:
    """丢弃最早保留的历史片段，为更高价值的当前上下文腾出预算。"""

    candidates = [
        item for item in working if item.fragment.kind == "history" and item.retained_tokens > 0
    ]
    if not candidates:
        return False
    target = min(candidates, key=lambda item: item.index)
    target.retained_tokens = 0
    target.status = "dropped"
    target.fallback_decision = "history trimmed before lower-trust context truncation"
    return True


def _truncate_next_retrieval_or_tool(
    working: list[_WorkingFragment],
    token_budget: int,
) -> bool:
    """缩短最大的检索或工具片段，但至少保留一个 token 以维持可解释的来源痕迹。"""

    candidates = [
        item
        for item in working
        if item.fragment.kind in {"retrieval", "tool_output"} and item.retained_tokens > 1
    ]
    if not candidates:
        return False
    excess = max(_used_tokens(working) - token_budget, 1)
    # 先压缩最长且最晚进入的候选，减少小片段被反复切割造成的不可预测结果。
    target = max(candidates, key=lambda item: (item.retained_tokens, -item.index))
    target.retained_tokens = max(1, target.retained_tokens - excess)
    target.status = "truncated"
    target.fallback_decision = "retrieval/tool output truncated after history trim"
    return True


def _drop_lowest_priority_fragment(working: list[_WorkingFragment]) -> bool:
    """在前两类可降级内容耗尽后，丢弃优先级最低且最早出现的剩余片段。"""

    candidates = [item for item in working if item.retained_tokens > 0]
    if not candidates:
        return False
    target = min(candidates, key=lambda item: (item.fragment.priority, item.index))
    target.retained_tokens = 0
    target.status = "dropped"
    target.fallback_decision = "lowest-priority context dropped"
    return True


def _used_tokens(working: list[_WorkingFragment]) -> int:
    """计算当前所有仍保留片段的 token 估算总和，作为预算循环的唯一度量。"""

    return sum(item.retained_tokens for item in working)


def _truncate_content(content: str, retained_tokens: int, original_tokens: int) -> str:
    """按 token 保留比例近似裁剪字符内容；真实 tokenization 由上游估算边界负责。"""

    if original_tokens <= 0:
        return ""
    ratio = retained_tokens / original_tokens
    keep_chars = max(1, int(len(content) * ratio))
    return content[:keep_chars].rstrip()


def _trace_fragment(item: _WorkingFragment) -> ContextFragmentTrace:
    """将内部工作项投影为公开 trace，保留来源、信任、预算前后值和降级原因。"""

    return ContextFragmentTrace(
        source_ref=item.fragment.source_ref,
        kind=item.fragment.kind,
        trust_level=item.fragment.trust_level,
        original_tokens=item.original_tokens,
        retained_tokens=item.retained_tokens,
        status=item.status,
        artifact_ref=item.fragment.artifact_ref,
        fallback_decision=item.fallback_decision,
    )


def _assembly_decision(traces: list[ContextFragmentTrace]) -> str | None:
    """从逐片段 trace 汇总最严重的降级结果，供调用方快速展示而不丢失明细。"""

    if any(trace.status == "dropped" for trace in traces):
        return "trimmed"
    if any(trace.status == "truncated" for trace in traces):
        return "truncated"
    return None
