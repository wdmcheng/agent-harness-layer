"""Hybrid retrieval 与 Reciprocal Rank Fusion。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TypedDict

from agent_harness.retrieval.provider import RetrievalResult


class RRFContribution(TypedDict):
    provider: str
    set: str
    rank: int
    score: float


@dataclass
class _RRFState:
    base: RetrievalResult
    score: float = 0.0
    contributions: list[RRFContribution] = field(default_factory=lambda: [])


def merge_rrf(
    result_sets: Mapping[str, Sequence[RetrievalResult]],
    *,
    k: int = 60,
    top_k: int | None = None,
) -> list[RetrievalResult]:
    """用 RRF 合并多 provider rank，并保留每个 provider 的贡献证据。"""

    merged: dict[tuple[str, str], _RRFState] = {}
    for provider_name, results in result_sets.items():
        for result in results:
            key = (result.document_id, result.chunk_id)
            state = merged.setdefault(key, _RRFState(base=result))
            state.score += 1 / (k + result.rank)
            state.contributions.append(
                {
                    "provider": result.provider,
                    "set": provider_name,
                    "rank": result.rank,
                    "score": result.score,
                }
            )
    ranked = sorted(
        merged.values(),
        key=lambda state: (-state.score, _first_rank(state), state.base.chunk_id),
    )
    output: list[RetrievalResult] = []
    for rank, state in enumerate(ranked[:top_k], start=1):
        base = state.base
        output.append(
            base.model_copy(
                update={
                    "provider": "hybrid-rrf",
                    "score": state.score,
                    "rank": rank,
                    "metadata": {
                        **base.metadata,
                        "rrf_k": k,
                        "rrf_contributions": state.contributions,
                    },
                }
            )
        )
    return output


def _first_rank(state: _RRFState) -> int:
    return min(item["rank"] for item in state.contributions)
