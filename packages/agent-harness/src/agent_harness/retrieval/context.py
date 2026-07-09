"""Retrieval result 到 ContextAssembler 输入的转换。"""

from __future__ import annotations

from agent_harness.context import ContextFragment
from agent_harness.retrieval.provider import RetrievalResult


def retrieval_result_to_context_fragment(
    result: RetrievalResult,
    *,
    token_estimate: int | None = None,
) -> ContextFragment:
    """把检索结果包装成引用内容，避免 untrusted 文本冒充高优先级指令。"""

    estimate = token_estimate if token_estimate is not None else _estimate_tokens(result.content)
    wrapped = (
        "[检索引用]\n"
        f"citation: {result.citation}\n"
        f"source_ref: {result.source_ref}\n"
        f"trust_level: {result.trust_level}\n"
        "引用内容:\n"
        f"{result.content}"
    )
    return ContextFragment(
        source_ref=result.source_ref,
        trust_level=result.trust_level,
        content=wrapped,
        token_estimate=estimate,
        kind="retrieval",
        priority=50,
    )


def retrieval_results_to_context_fragments(
    results: list[RetrievalResult],
) -> list[ContextFragment]:
    """批量转换检索结果，保留原 rank 顺序。"""

    return [retrieval_result_to_context_fragment(result) for result in results]


def _estimate_tokens(content: str) -> int:
    return max(1, len(content) // 4)
